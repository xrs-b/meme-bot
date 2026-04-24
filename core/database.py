#!/usr/bin/env python3
"""
SQLite Database with TTL support for Meme Bot
Automatically cleans up old data
"""

import sqlite3
import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

from .models import Token, Pool, Signal, Trade, WalletPosition, Chain, SignalType


class Database:
    """SQLite database with automatic TTL cleanup"""
    
    def __init__(self, db_path: str = "meme_bot.db", ttl_days: int = 7):
        self.db_path = Path(db_path)
        self.ttl_days = ttl_days
        self._lock = threading.Lock()
        self._init_db()
        
    def _init_db(self):
        """Initialize database schema"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Tokens table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tokens (
                    address TEXT PRIMARY KEY,
                    chain TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT,
                    decimals INTEGER,
                    logo_url TEXT,
                    created_at TEXT,
                    is_meme INTEGER DEFAULT 1
                )
            """)
            
            # Pools table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pools (
                    address TEXT PRIMARY KEY,
                    token_address TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    reserve_usd REAL,
                    reserve_token REAL,
                    reserve_quote REAL,
                    volume_24h REAL,
                    price REAL,
                    created_at TEXT,
                    updated_at TEXT,
                    FOREIGN KEY (token_address) REFERENCES tokens(address)
                )
            """)
            
            # Signals table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    token_address TEXT NOT NULL,
                    message TEXT,
                    confidence REAL,
                    score INTEGER,
                    price_at_signal REAL,
                    liquidity_at_signal REAL,
                    volume_24h_at_signal REAL,
                    source_address TEXT,
                    detected_at TEXT,
                    metadata TEXT,
                    FOREIGN KEY (token_address) REFERENCES tokens(address)
                )
            """)
            
            # Trades table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id TEXT PRIMARY KEY,
                    chain TEXT NOT NULL,
                    action TEXT NOT NULL,
                    token_address TEXT NOT NULL,
                    token_symbol TEXT,
                    amount_in REAL,
                    amount_out REAL,
                    price REAL,
                    value_usd REAL,
                    tx_hash TEXT,
                    wallet_address TEXT,
                    signal_id TEXT,
                    executed_at TEXT,
                    pnl_percent REAL,
                    pnl_usd REAL
                )
            """)
            
            # Wallet positions table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS wallet_positions (
                    wallet_address TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    token_address TEXT NOT NULL,
                    token_symbol TEXT,
                    balance REAL,
                    value_usd REAL,
                    avg_buy_price REAL,
                    current_price REAL,
                    unrealized_pnl_percent REAL,
                    unrealized_pnl_usd REAL,
                    updated_at TEXT,
                    PRIMARY KEY (wallet_address, token_address)
                )
            """)
            
            # Config table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT
                )
            """)
            
            # Indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_detected ON signals(detected_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_executed ON trades(executed_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_wallet ON wallet_positions(wallet_address)")
            
            conn.commit()
    
    @contextmanager
    def get_connection(self):
        """Thread-safe database connection"""
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            with self._lock:
                yield conn
        finally:
            conn.close()
    
    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """Convert row to dict, parsing JSON fields"""
        if row is None:
            return None
        d = dict(row)
        # Parse JSON fields
        for key in ['metadata']:
            if key in d and d[key]:
                try:
                    d[key] = json.loads(d[key])
                except:
                    pass
        return d
    
    # ============ TOKEN OPERATIONS ============
    
    def save_token(self, token: Token):
        """Save or update a token"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO tokens 
                (address, chain, symbol, name, decimals, logo_url, created_at, is_meme)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                token.address, token.chain.value, token.symbol, token.name,
                token.decimals, token.logo_url, token.created_at.isoformat(), token.is_meme
            ))
            conn.commit()
    
    def get_token(self, address: str, chain: Chain) -> Optional[Token]:
        """Get a token by address and chain"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM tokens WHERE address = ? AND chain = ?",
                (address, chain.value)
            )
            row = cursor.fetchone()
            if row:
                d = self._row_to_dict(row)
                return Token(
                    symbol=d['symbol'], name=d['name'], address=d['address'],
                    chain=Chain(d['chain']), decimals=d['decimals'],
                    logo_url=d['logo_url'], created_at=datetime.fromisoformat(d['created_at']),
                    is_meme=bool(d['is_meme'])
                )
            return None
    
    def get_recent_tokens(self, chain: Chain, limit: int = 50) -> List[Token]:
        """Get recent tokens for a chain"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM tokens WHERE chain = ? ORDER BY created_at DESC LIMIT ?",
                (chain.value, limit)
            )
            tokens = []
            for row in cursor.fetchall():
                d = self._row_to_dict(row)
                tokens.append(Token(
                    symbol=d['symbol'], name=d['name'], address=d['address'],
                    chain=Chain(d['chain']), decimals=d['decimals'],
                    logo_url=d['logo_url'], created_at=datetime.fromisoformat(d['created_at']),
                    is_meme=bool(d['is_meme'])
                ))
            return tokens
    
    # ============ POOL OPERATIONS ============
    
    def save_pool(self, pool: Pool):
        """Save or update a pool"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO pools 
                (address, token_address, chain, reserve_usd, reserve_token, reserve_quote,
                 volume_24h, price, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pool.address, pool.token_address, pool.chain.value,
                pool.reserve_usd, pool.reserve_token, pool.reserve_quote,
                pool.volume_24h, pool.price, pool.created_at.isoformat(),
                pool.updated_at.isoformat()
            ))
            conn.commit()
    
    def get_pool(self, address: str) -> Optional[Pool]:
        """Get a pool by address"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM pools WHERE address = ?", (address,))
            row = cursor.fetchone()
            if row:
                d = self._row_to_dict(row)
                return Pool(
                    address=d['address'], token_address=d['token_address'],
                    chain=Chain(d['chain']), reserve_usd=d['reserve_usd'],
                    reserve_token=d['reserve_token'], reserve_quote=d['reserve_quote'],
                    volume_24h=d['volume_24h'], price=d['price'],
                    created_at=datetime.fromisoformat(d['created_at']),
                    updated_at=datetime.fromisoformat(d['updated_at'])
                )
            return None
    
    # ============ SIGNAL OPERATIONS ============
    
    def save_signal(self, signal: Signal):
        """Save a trading signal"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO signals 
                (id, type, chain, token_address, message, confidence, score,
                 price_at_signal, liquidity_at_signal, volume_24h_at_signal,
                 source_address, detected_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal.id, signal.type.value, signal.chain.value,
                signal.token.address, signal.message, signal.confidence, signal.score,
                signal.price_at_signal, signal.liquidity_at_signal, signal.volume_24h_at_signal,
                signal.source_address, signal.detected_at.isoformat(),
                json.dumps(signal.metadata)
            ))
            conn.commit()
    
    def get_signal(self, signal_id: str) -> Optional[Signal]:
        """Get a signal by ID"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM signals WHERE id = ?", (signal_id,))
            row = cursor.fetchone()
            if row:
                return self._row_to_signal(row)
            return None
    
    def get_recent_signals(self, chain: Chain, hours: int = 24, min_score: int = 0) -> List[Signal]:
        """Get recent signals for a chain"""
        since = datetime.utcnow() - timedelta(hours=hours)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM signals 
                WHERE chain = ? AND detected_at > ? AND score >= ?
                ORDER BY detected_at DESC
            """, (chain.value, since.isoformat(), min_score))
            signals = []
            for row in cursor.fetchall():
                signals.append(self._row_to_signal(row))
            return signals
    
    def _row_to_signal(self, row: sqlite3.Row) -> Signal:
        """Convert row to Signal object"""
        d = self._row_to_dict(row)
        token = self.get_token(d['token_address'], Chain(d['chain']))
        if token is None:
            token = Token(
                symbol="UNKNOWN", name="Unknown", address=d['token_address'],
                chain=Chain(d['chain'])
            )
        return Signal(
            id=d['id'], type=SignalType(d['type']), chain=Chain(d['chain']),
            token=token, message=d['message'], confidence=d['confidence'],
            score=d['score'], price_at_signal=d['price_at_signal'],
            liquidity_at_signal=d['liquidity_at_signal'],
            volume_24h_at_signal=d['volume_24h_at_signal'],
            source_address=d['source_address'],
            detected_at=datetime.fromisoformat(d['detected_at']),
            metadata=d.get('metadata', {})
        )
    
    # ============ TRADE OPERATIONS ============
    
    def save_trade(self, trade: Trade):
        """Save an executed trade"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trades 
                (id, chain, action, token_address, token_symbol, amount_in, amount_out,
                 price, value_usd, tx_hash, wallet_address, signal_id, executed_at,
                 pnl_percent, pnl_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.id, trade.chain.value, trade.action.value, trade.token_address,
                trade.token_symbol, trade.amount_in, trade.amount_out, trade.price,
                trade.value_usd, trade.tx_hash, trade.wallet_address, trade.signal_id,
                trade.executed_at.isoformat(), trade.pnl_percent, trade.pnl_usd
            ))
            conn.commit()
    
    def get_recent_trades(self, chain: Chain, hours: int = 24) -> List[Trade]:
        """Get recent trades"""
        since = datetime.utcnow() - timedelta(hours=hours)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM trades 
                WHERE chain = ? AND executed_at > ?
                ORDER BY executed_at DESC
            """, (chain.value, since.isoformat()))
            trades = []
            for row in cursor.fetchall():
                d = self._row_to_dict(row)
                trades.append(Trade(
                    id=d['id'], chain=Chain(d['chain']), action=TradeAction(d['action']),
                    token_address=d['token_address'], token_symbol=d['token_symbol'],
                    amount_in=d['amount_in'], amount_out=d['amount_out'], price=d['price'],
                    value_usd=d['value_usd'], tx_hash=d['tx_hash'],
                    wallet_address=d['wallet_address'], signal_id=d['signal_id'],
                    executed_at=datetime.fromisoformat(d['executed_at']),
                    pnl_percent=d['pnl_percent'], pnl_usd=d['pnl_usd']
                ))
            return trades
    
    # ============ WALLET POSITION OPERATIONS ============
    
    def save_wallet_position(self, pos: WalletPosition):
        """Save or update wallet position"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO wallet_positions 
                (wallet_address, chain, token_address, token_symbol, balance,
                 value_usd, avg_buy_price, current_price, unrealized_pnl_percent,
                 unrealized_pnl_usd, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pos.wallet_address, pos.chain.value, pos.token_address,
                pos.token_symbol, pos.balance, pos.value_usd, pos.avg_buy_price,
                pos.current_price, pos.unrealized_pnl_percent, pos.unrealized_pnl_usd,
                pos.updated_at.isoformat()
            ))
            conn.commit()
    
    def get_wallet_positions(self, wallet_address: str, chain: Chain) -> List[WalletPosition]:
        """Get all positions for a wallet"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM wallet_positions 
                WHERE wallet_address = ? AND chain = ?
                ORDER BY value_usd DESC
            """, (wallet_address, chain.value))
            positions = []
            for row in cursor.fetchall():
                d = self._row_to_dict(row)
                positions.append(WalletPosition(
                    wallet_address=d['wallet_address'], chain=Chain(d['chain']),
                    token_address=d['token_address'], token_symbol=d['token_symbol'],
                    balance=d['balance'], value_usd=d['value_usd'],
                    avg_buy_price=d['avg_buy_price'], current_price=d['current_price'],
                    unrealized_pnl_percent=d['unrealized_pnl_percent'],
                    unrealized_pnl_usd=d['unrealized_pnl_usd'],
                    updated_at=datetime.fromisoformat(d['updated_at'])
                ))
            return positions
    
    # ============ CONFIG OPERATIONS ============
    
    def save_config(self, key: str, value: Any):
        """Save config value"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO config (key, value, updated_at)
                VALUES (?, ?, ?)
            """, (key, json.dumps(value), datetime.utcnow().isoformat()))
            conn.commit()
    
    def get_config(self, key: str, default: Any = None) -> Any:
        """Get config value"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row:
                try:
                    return json.loads(row['value'])
                except:
                    return row['value']
            return default
    
    # ============ CLEANUP ============
    
    def cleanup_old_data(self):
        """Remove data older than TTL"""
        cutoff = datetime.utcnow() - timedelta(days=self.ttl_days)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Clean signals
            cursor.execute("DELETE FROM signals WHERE detected_at < ?", (cutoff.isoformat(),))
            
            # Clean trades
            cursor.execute("DELETE FROM trades WHERE executed_at < ?", (cutoff.isoformat(),))
            
            # Clean old positions (keep recent)
            cursor.execute("DELETE FROM wallet_positions WHERE updated_at < ?", (cutoff.isoformat(),))
            
            # Vacuum to reclaim space
            conn.commit()
            conn.execute("VACUUM")
            
            return cursor.rowcount
    
    # ============ STATS ============
    
    def get_stats(self, chain: Chain) -> Dict[str, Any]:
        """Get stats for a chain"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Count signals
            cursor.execute("SELECT COUNT(*) FROM signals WHERE chain = ?", (chain.value,))
            signal_count = cursor.fetchone()[0]
            
            # Count trades
            cursor.execute("SELECT COUNT(*) FROM trades WHERE chain = ?", (chain.value,))
            trade_count = cursor.fetchone()[0]
            
            # Sum P&L
            cursor.execute("""
                SELECT SUM(pnl_usd) FROM trades 
                WHERE chain = ? AND pnl_usd IS NOT NULL
            """, (chain.value,))
            total_pnl = cursor.fetchone()[0] or 0.0
            
            # Win rate
            cursor.execute("""
                SELECT COUNT(*) FROM trades 
                WHERE chain = ? AND action = 'sell' AND pnl_percent > 0
            """, (chain.value,))
            wins = cursor.fetchone()[0]
            cursor.execute("""
                SELECT COUNT(*) FROM trades 
                WHERE chain = ? AND action = 'sell' AND pnl_percent IS NOT NULL
            """, (chain.value,))
            total_sells = cursor.fetchone()[0]
            
            return {
                'signal_count': signal_count,
                'trade_count': trade_count,
                'total_pnl': total_pnl,
                'win_rate': (wins / total_sells * 100) if total_sells > 0 else 0
            }
