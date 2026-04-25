#!/usr/bin/env python3
"""
Data models for Meme Bot
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any
from decimal import Decimal


class Chain(Enum):
    SOLANA = "solana"
    BSC = "bsc"


class SignalType(Enum):
    NEW_COIN = "new_coin"
    LIQUIDITY_INCREASE = "liquidity_increase"
    VOLUME_SPIKE = "volume_spike"
    PRICE_PUMP = "price_pump"
    WALLET_ACTIVITY = "wallet_activity"
    RUG_PULL = "rug_pull"
    NEW_POOL = "new_pool"


class TradeAction(Enum):
    BUY = "buy"
    SELL = "sell"


class AlertMode(Enum):
    NOTIFY_ONLY = "notify_only"
    AUTO_TRADE = "auto_trade"


@dataclass
class Token:
    """Token information"""
    symbol: str
    name: str
    address: str
    chain: Chain
    decimals: int = 9
    logo_url: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    is_meme: bool = True


@dataclass
class Pool:
    """Liquidity pool information"""
    address: str
    token_address: str
    chain: Chain
    reserve_usd: float
    reserve_token: float
    reserve_quote: float  # reserve of SOL/BNB/USDT
    volume_24h: float
    price: float
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Signal:
    """Trading signal"""
    id: str
    type: SignalType
    chain: Chain
    token: Token
    pool: Optional[Pool] = None
    
    # Signal details
    message: str = ""
    confidence: float = 0.0  # 0.0 - 1.0
    score: int = 0  # 1-100, higher = more attractive
    
    # Metrics at signal time
    price_at_signal: float = 0.0
    liquidity_at_signal: float = 0.0
    volume_24h_at_signal: float = 0.0
    
    # Source
    source_address: Optional[str] = None  # Wallet that triggered or pool creator
    detected_at: datetime = field(default_factory=datetime.utcnow)
    
    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Trade:
    """Executed trade"""
    id: str
    chain: Chain
    action: TradeAction
    token_address: str
    token_symbol: str
    
    amount_in: float  # Amount of SOL/BNB/USDT spent
    amount_out: float  # Amount of tokens received
    price: float
    value_usd: float
    
    tx_hash: str
    wallet_address: str
    
    # Timing
    signal_id: Optional[str] = None
    executed_at: datetime = field(default_factory=datetime.utcnow)
    
    # P&L (for sells)
    pnl_percent: Optional[float] = None
    pnl_usd: Optional[float] = None


@dataclass
class WalletPosition:
    """Current wallet position"""
    wallet_address: str
    chain: Chain
    token_address: str
    token_symbol: str
    
    balance: float
    value_usd: float
    avg_buy_price: float
    current_price: float
    
    unrealized_pnl_percent: float = 0.0
    unrealized_pnl_usd: float = 0.0
    
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class BotConfig:
    """Bot configuration"""
    # Chain settings
    solana_rpc: str = ""
    solana_ws: str = ""
    bsc_rpc: str = ""
    
    # Wallet
    wallet_address: str = ""
    wallet_private_key: str = ""  # Encrypted in production!
    
    # Trading
    default_slippage: float = 5.0  # %
    default_amount_per_trade: float = 0.1  # SOL/BNB
    max_position_per_token: float = 1.0  # SOL/BNB
    max_total_position: float = 10.0  # SOL/BNB
    
    # Modes
    alert_mode: AlertMode = AlertMode.NOTIFY_ONLY
    
    # Signal filters
    min_liquidity: float = 1000.0  # USD
    min_volume_24h: float = 5000.0  # USD
    min_signal_score: int = 60  # Only alert if score >= this
    
    # Copy trading
    followed_wallets: List[str] = field(default_factory=list)
    copy_trade_enabled: bool = False
    copy_trade_amount_percent: float = 50.0  # % of followed wallet's trade size
    
    # Persistence
    data_ttl_days: int = 7
    
    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    
    # Pools to monitor (contract addresses)
    sol_pools: List[str] = field(default_factory=list)
    bsc_pools: List[str] = field(default_factory=list)


# Alias for backwards compatibility
Position = WalletPosition

