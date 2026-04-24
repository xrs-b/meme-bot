#!/usr/bin/env python3
"""
Signal Detection Engine for Meme Bot
Monitors chain data and generates trading signals
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass, field
import logging

from .models import (
    Token, Pool, Signal, Chain, SignalType, 
    BotConfig, AlertMode
)
from .database import Database
from .alert_manager import AlertManager

logger = logging.getLogger(__name__)


@dataclass
class SignalFilter:
    """Filters for signal detection"""
    min_liquidity: float = 1000.0  # USD
    min_volume_24h: float = 5000.0  # USD
    min_score: int = 60
    max_age_hours: int = 24


class SignalDetector:
    """
    Core signal detection engine.
    Monitors pools and generates trading signals based on configurable rules.
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
        self.filters = SignalFilter(
            min_liquidity=config.min_liquidity,
            min_volume_24h=config.min_volume_24h,
            min_score=config.min_signal_score
        )
        
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._callbacks: List[Callable[[Signal], None]] = []
        
        # Track known pools to detect new ones
        self._known_pools: Dict[str, datetime] = {}
        self._known_tokens: Dict[str, datetime] = {}
        
        # Historical data for pattern detection
        self._liquidity_history: Dict[str, List[float]] = {}
        self._volume_history: Dict[str, List[float]] = {}
        self._price_history: Dict[str, List[float]] = {}
    
    def add_callback(self, callback: Callable[[Signal], None]):
        """Add a callback to be called when signals are detected"""
        self._callbacks.append(callback)
    
    async def start(self):
        """Start the signal detection engine"""
        self._running = True
        logger.info("SignalDetector started")
        
        # Start periodic cleanup task
        cleanup_task = asyncio.create_task(self._periodic_cleanup())
        self._tasks.append(cleanup_task)
    
    async def stop(self):
        """Stop the signal detection engine"""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("SignalDetector stopped")
    
    async def _periodic_cleanup(self):
        """Periodic cleanup of old data"""
        while self._running:
            try:
                await asyncio.sleep(3600)  # Every hour
                if not self._running:
                    break
                count = self.db.cleanup_old_data()
                logger.info(f"Cleaned up {count} old records")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
    
    async def check_new_pool(
        self, 
        pool_address: str, 
        token: Token, 
        pool_data: Dict[str, Any]
    ) -> Optional[Signal]:
        """
        Check if a pool is new and worth alerting.
        Called by chain adapters when new pools are detected.
        """
        now = datetime.utcnow()
        
        # Skip if we already know about this pool
        if pool_address in self._known_pools:
            return None
        
        # Mark as known
        self._known_pools[pool_address] = now
        
        # Parse pool data
        liquidity = pool_data.get('reserve_usd', 0)
        volume_24h = pool_data.get('volume_24h', 0)
        price = pool_data.get('price', 0)
        
        # Create pool record
        pool = Pool(
            address=pool_address,
            token_address=token.address,
            chain=token.chain,
            reserve_usd=liquidity,
            reserve_token=pool_data.get('reserve_token', 0),
            reserve_quote=pool_data.get('reserve_quote', 0),
            volume_24h=volume_24h,
            price=price,
            created_at=now,
            updated_at=now
        )
        self.db.save_pool(pool)
        
        # Save token
        self.db.save_token(token)
        
        # Check minimum filters
        if liquidity < self.filters.min_liquidity:
            logger.debug(f"Pool {pool_address} filtered: low liquidity ${liquidity}")
            return None
        
        # Calculate signal score
        score = self._calculate_signal_score(
            liquidity=liquidity,
            volume_24h=volume_24h,
            age_hours=0,
            has_meme_symbol=self._is_likely_meme(token.symbol)
        )
        
        # Create signal
        signal = Signal(
            id=str(uuid.uuid4()),
            type=SignalType.NEW_POOL if liquidity > 5000 else SignalType.NEW_COIN,
            chain=token.chain,
            token=token,
            pool=pool,
            message=self._generate_signal_message(token, pool, score),
            confidence=min(0.5 + (liquidity / 50000) * 0.5, 0.95),
            score=score,
            price_at_signal=price,
            liquidity_at_signal=liquidity,
            volume_24h_at_signal=volume_24h,
            source_address=pool_data.get('creator', ''),
            metadata={
                'pool_address': pool_address,
                'pool_data': pool_data
            }
        )
        
        # Save signal
        self.db.save_signal(signal)
        
        # Trigger callbacks
        for callback in self._callbacks:
            try:
                callback(signal)
            except Exception as e:
                logger.error(f"Callback error: {e}")
        
        return signal
    
    async def check_liquidity_change(
        self,
        pool_address: str,
        new_liquidity: float,
        pool_data: Dict[str, Any]
    ) -> Optional[Signal]:
        """
        Check if liquidity has changed significantly.
        Called by chain adapters on pool updates.
        """
        pool = self.db.get_pool(pool_address)
        if not pool:
            return None
        
        old_liquidity = pool.reserve_usd
        liquidity_change = (new_liquidity - old_liquidity) / old_liquidity if old_liquidity > 0 else 0
        
        # Track history
        if pool_address not in self._liquidity_history:
            self._liquidity_history[pool_address] = []
        self._liquidity_history[pool_address].append(new_liquidity)
        
        # Detect significant increase (potential pump signal)
        if liquidity_change > 0.5:  # 50% increase
            token = self.db.get_token(pool.token_address, pool.chain)
            if not token:
                return None
            
            score = min(int(liquidity_change * 50) + 50, 100)
            
            signal = Signal(
                id=str(uuid.uuid4()),
                type=SignalType.LIQUIDITY_INCREASE,
                chain=pool.chain,
                token=token,
                pool=pool,
                message=f"💧 Liquidity increased by {liquidity_change:.1%}!\n"
                       f"Old: ${old_liquidity:,.0f} → New: ${new_liquidity:,.0f}",
                confidence=min(0.4 + liquidity_change * 0.3, 0.9),
                score=score,
                price_at_signal=pool_data.get('price', pool.price),
                liquidity_at_signal=new_liquidity,
                volume_24h_at_signal=pool_data.get('volume_24h', pool.volume_24h),
                metadata={
                    'old_liquidity': old_liquidity,
                    'new_liquidity': new_liquidity,
                    'change_percent': liquidity_change * 100
                }
            )
            
            self.db.save_signal(signal)
            
            for callback in self._callbacks:
                try:
                    callback(signal)
                except Exception as e:
                    logger.error(f"Callback error: {e}")
            
            return signal
        
        # Update pool record
        pool.reserve_usd = new_liquidity
        pool.updated_at = datetime.utcnow()
        self.db.save_pool(pool)
        
        return None
    
    async def check_volume_spike(
        self,
        pool_address: str,
        new_volume: float,
        pool_data: Dict[str, Any]
    ) -> Optional[Signal]:
        """Check for unusual volume spikes"""
        pool = self.db.get_pool(pool_address)
        if not pool:
            return None
        
        old_volume = pool.volume_24h
        volume_change = (new_volume - old_volume) / old_volume if old_volume > 0 else 0
        
        if volume_change > 2.0:  # 200% increase
            token = self.db.get_token(pool.token_address, pool.chain)
            if not token:
                return None
            
            score = min(int(volume_change * 30) + 40, 100)
            
            signal = Signal(
                id=str(uuid.uuid4()),
                type=SignalType.VOLUME_SPIKE,
                chain=pool.chain,
                token=token,
                pool=pool,
                message=f"📈 Volume spike detected!\n"
                       f"24h Volume: ${old_volume:,.0f} → ${new_volume:,.0f}",
                confidence=min(0.5 + volume_change * 0.1, 0.9),
                score=score,
                price_at_signal=pool_data.get('price', pool.price),
                liquidity_at_signal=pool_data.get('reserve_usd', pool.reserve_usd),
                volume_24h_at_signal=new_volume
            )
            
            self.db.save_signal(signal)
            
            for callback in self._callbacks:
                try:
                    callback(signal)
                except Exception as e:
                    logger.error(f"Callback error: {e}")
            
            return signal
        
        return None
    
    async def check_rug_pull(
        self,
        pool_address: str,
        pool_data: Dict[str, Any]
    ) -> Optional[Signal]:
        """Check for potential rug pull patterns"""
        pool = self.db.get_pool(pool_address)
        if not pool:
            return None
        
        new_liquidity = pool_data.get('reserve_usd', pool.reserve_usd)
        liquidity_drop = (pool.reserve_usd - new_liquidity) / pool.reserve_usd if pool.reserve_usd > 0 else 0
        
        # Rug pull detected if liquidity dropped significantly
        if liquidity_drop > 0.5:  # 50% liquidity gone
            token = self.db.get_token(pool.token_address, pool.chain)
            if not token:
                return None
            
            signal = Signal(
                id=str(uuid.uuid4()),
                type=SignalType.RUG_PULL,
                chain=pool.chain,
                token=token,
                pool=pool,
                message=f"⚠️ WARNING: {liquidity_drop:.0%} of liquidity removed!\n"
                       f"Liquidity: ${pool.reserve_usd:,.0f} → ${new_liquidity:,.0f}\n"
                       f"URGENT: Check your position!",
                confidence=0.95,
                score=100,
                price_at_signal=pool_data.get('price', pool.price),
                liquidity_at_signal=new_liquidity,
                volume_24h_at_signal=pool_data.get('volume_24h', pool.volume_24h)
            )
            
            self.db.save_signal(signal)
            
            # Alert immediately for rug pulls
            await self.alert_manager.alert_rug_pull(signal)
            
            return signal
        
        return None
    
    def _calculate_signal_score(
        self,
        liquidity: float,
        volume_24h: float,
        age_hours: int,
        has_meme_symbol: bool
    ) -> int:
        """
        Calculate a signal score from 0-100.
        Higher = more attractive opportunity.
        """
        score = 0
        
        # Liquidity score (up to 30 points)
        if liquidity > 100000:
            score += 30
        elif liquidity > 50000:
            score += 25
        elif liquidity > 10000:
            score += 20
        elif liquidity > 5000:
            score += 15
        elif liquidity > 1000:
            score += 10
        
        # Volume score (up to 25 points)
        if volume_24h > 100000:
            score += 25
        elif volume_24h > 50000:
            score += 20
        elif volume_24h > 10000:
            score += 15
        elif volume_24h > 5000:
            score += 10
        
        # Age score (up to 15 points) - newer = more opportunity
        if age_hours < 1:
            score += 15
        elif age_hours < 6:
            score += 10
        elif age_hours < 24:
            score += 5
        
        # Meme symbol bonus (up to 15 points)
        if has_meme_symbol:
            score += 15
        
        # Volume/Liquidity ratio bonus (up to 15 points)
        if liquidity > 0:
            ratio = volume_24h / liquidity
            if ratio > 2:
                score += 15
            elif ratio > 1:
                score += 10
            elif ratio > 0.5:
                score += 5
        
        return min(score, 100)
    
    def _is_likely_meme(self, symbol: str) -> bool:
        """Simple heuristic to detect if a token symbol looks like a meme coin"""
        symbol_lower = symbol.lower()
        
        # Known meme patterns
        meme_patterns = [
            'doge', 'shib', 'pepe', 'floki', 'bonk', 'wif', 'dog', 'cat',
            'moon', 'inu', 'akita', 'sam', 'elon', 'baby', 'mini', 'micro',
            'safe', 'feg', 'pit', 'hoge', 'samo', 'orca', 'luna', 'ash',
            'people', 'GME', 'AMC', 'DOGE', 'SHIB', 'PEPE', 'FLOKI'
        ]
        
        for pattern in meme_patterns:
            if pattern.lower() in symbol_lower:
                return True
        
        # Check if all letters are the same (common meme pattern)
        if len(symbol) > 2 and len(set(symbol.lower())) <= 3:
            return True
        
        return False
    
    def _generate_signal_message(self, token: Token, pool: Pool, score: int) -> str:
        """Generate a human-readable signal message"""
        score_emoji = "🟢" if score >= 80 else "🟡" if score >= 60 else "🔴"
        
        msg = f"""
🆕 *NEW MEME COIN DETECTED*

🏷️ *{token.name}* ({token.symbol})
📛 Contract: `{token.address[:20]}...`

{score_emoji} Signal Score: *{score}/100*
💧 Liquidity: ${pool.reserve_usd:,.0f}
📊 24h Volume: ${pool.volume_24h:,.0f}
💰 Price: ${pool.price:,.8f}
"""
        return msg.strip()
    
    def get_recent_signals(self, chain: Chain, hours: int = 24) -> List[Signal]:
        """Get recent signals for a chain"""
        return self.db.get_recent_signals(chain, hours, self.filters.min_score)
    
    def get_signal_stats(self, chain: Chain) -> Dict[str, Any]:
        """Get signal statistics"""
        return self.db.get_stats(chain)
