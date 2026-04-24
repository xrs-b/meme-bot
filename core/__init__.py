#!/usr/bin/env python3
"""
Meme Bot - Cross-chain Meme Coin Monitor & Trading Bot
Supports: Solana (SOL) & Binance Smart Chain (BSC)
Features: New coin alerts, liquidity monitoring, copy trading, auto-swap, Telegram alerts
"""

from core.signal_detector import SignalDetector
from core.trading_engine import TradingEngine
from core.copy_trader import CopyTrader
from core.alert_manager import AlertManager
from core.database import Database

__version__ = "1.0.0"
__author__ = "Meme Bot Team"

__all__ = [
    "SignalDetector",
    "TradingEngine", 
    "CopyTrader",
    "AlertManager",
    "Database",
]