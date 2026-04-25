"""
仓位管理器
追踪所有活跃持仓，定期检查止盈/止损/跟踪止损
"""
import sqlite3
import uuid
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import List, Optional, Callable
from enum import Enum

logger = logging.getLogger(__name__)

DB_PATH = "/root/.openclaw/workspace/meme-bot/meme_bot.db"


class PositionStatus(Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    STOPPED = "STOPPED"     # 止损触发
    TAKEN_PROFIT = "TAKEN_PROFIT"  # 止盈触发
    EXPIRED = "EXPIRED"    # 确认超时


@dataclass
class Position:
    id: str
    token_address: str
    symbol: str
    name: str
    chain: str
    stage: str            # 'NEW' or 'MIGRATING'
    entry_price: float   # 买入价（USD）
    entry_time: str       # ISO 时间
    quantity: float       # 代币数量
    cost_sol: float       # 花费了多少 SOL
    cost_usd: float       # 花费了多少 USD（按买入时 SOL 价格换算）
    stop_loss_pct: float
    take_profit_pct: float
    trailing_pct: float
    highest_price: float   # 入场后的最高价
    score: int            # 入场时评分
    status: str           # OPEN/CLOSED/STOPPED/TAKEN_PROFIT
    close_time: Optional[str] = None
    close_price: Optional[float] = None
    pnl_sol: Optional[float] = None
    pnl_pct: Optional[float] = None
    note: Optional[str] = None
    sol_price_at_entry: float = 0  # 入场时 SOL/USD


class PositionManager:
    """仓位追踪核心类"""
    
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._ensure_tables()
        # 活跃持仓缓存（内存）
        self._positions: dict[str, Position] = {}
        self._load_open_positions()
    
    def _ensure_tables(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id TEXT PRIMARY KEY,
                token_address TEXT NOT NULL,
                symbol TEXT,
                name TEXT,
                chain TEXT,
                stage TEXT,
                entry_price REAL,
                entry_time TEXT,
                quantity REAL,
                cost_sol REAL,
                cost_usd REAL,
                stop_loss_pct REAL,
                take_profit_pct REAL,
                trailing_pct REAL,
                highest_price REAL,
                score INTEGER,
                status TEXT,
                close_time TEXT,
                close_price REAL,
                pnl_sol REAL,
                pnl_pct REAL,
                sol_price_at_entry REAL,
                note TEXT
            )
        """)
        conn.commit()
        conn.close()
    
    def _load_open_positions(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY entry_time DESC"
        ).fetchall()
        conn.close()
        
        cols = [desc[0] for desc in cur.description] if cur.description else []
        for row in rows:
            data = dict(zip(cols, row))
            p = Position(**data)
            self._positions[p.id] = p
        
        logger.info(f"Loaded {len(self._positions)} open positions from DB")
    
    # ─── 持仓操作 ───────────────────────────────────────────────
    
    def open_position(self, position: Position) -> bool:
        """开仓记录"""
        if position.id in self._positions:
            logger.warning(f"Position {position.id} already exists")
            return False
        
        self._positions[position.id] = position
        self._save_position(position)
        logger.info(f"Opened position: {position.symbol} @ {position.entry_price:.6f} (score={position.score})")
        return True
    
    def update_highest_price(self, position_id: str, current_price: float):
        """更新持仓最高价（用于跟踪止损）"""
        p = self._positions.get(position_id)
        if p and current_price > p.highest_price:
            p.highest_price = current_price
            self._save_position(p)
    
    def close_position(self, position_id: str, close_price: float,
                       reason: str = "MANUAL", pnl_sol: float = 0,
                       pnl_pct: float = 0) -> bool:
        """平仓"""
        p = self._positions.get(position_id)
        if not p:
            return False
        
        p.status = reason
        p.close_time = datetime.now(timezone.utc).isoformat()
        p.close_price = close_price
        p.pnl_sol = pnl_sol
        p.pnl_pct = pnl_pct
        
        self._save_position(p)
        del self._positions[position_id]
        logger.info(f"Closed position: {p.symbol} {reason} | PnL: {pnl_sol:.4f} SOL ({pnl_pct:+.1f}%)")
        return True
    
    def get_position(self, position_id: str) -> Optional[Position]:
        return self._positions.get(position_id)
    
    def get_position_by_token(self, token_address: str) -> Optional[Position]:
        """按合约地址查找活跃持仓"""
        for p in self._positions.values():
            if p.token_address == token_address:
                return p
        return None
    
    def get_all_open(self) -> List[Position]:
        return list(self._positions.values())
    
    def get_open_count(self, stage: str = None) -> int:
        if stage:
            return sum(1 for p in self._positions.values() if p.stage == stage)
        return len(self._positions)
    
    def get_total_cost_sol(self) -> float:
        return sum(p.cost_sol for p in self._positions.values())
    
    # ─── 数据库操作 ─────────────────────────────────────────────
    
    def _save_position(self, p: Position):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO positions 
            (id, token_address, symbol, name, chain, stage, entry_price, entry_time,
             quantity, cost_sol, cost_usd, stop_loss_pct, take_profit_pct, trailing_pct,
             highest_price, score, status, close_time, close_price, pnl_sol, pnl_pct,
             sol_price_at_entry, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            p.id, p.token_address, p.symbol, p.name, p.chain, p.stage,
            p.entry_price, p.entry_time, p.quantity, p.cost_sol, p.cost_usd,
            p.stop_loss_pct, p.take_profit_pct, p.trailing_pct, p.highest_price,
            p.score, p.status, p.close_time, p.close_price, p.pnl_sol, pnl_pct,
            p.sol_price_at_entry, p.note
        ))
        conn.commit()
        conn.close()
    
    # ─── 止盈/止损检查（供外部循环调用）─────────────────────────
    
    def check_conditions(self, token_address: str, current_price: float,
                        bonding_pct: float = 0) -> dict:
        """
        检查止盈止损条件
        返回: {
            'action': 'NONE' | 'TRAILING_STOP' | 'STOP_LOSS' | 'TAKE_PROFIT' | 'GRADUATED',
            'pnl_sol': float,
            'pnl_pct': float,
            'reason': str
        }
        """
        p = self.get_position_by_token(token_address)
        if not p or p.status != "OPEN":
            return {"action": "NONE"}
        
        entry = p.entry_price
        pnl_pct = ((current_price - entry) / entry) * 100
        sol_price = p.sol_price_at_entry or 1
        pnl_sol = p.cost_sol * (pnl_pct / 100)
        
        # 跟踪止损：最高点回撤超过阈值
        if p.highest_price > 0:
            trailing_drawdown = ((p.highest_price - current_price) / p.highest_price) * 100
            if trailing_drawdown >= p.trailing_pct:
                return {
                    "action": "TRAILING_STOP",
                    "pnl_sol": pnl_sol,
                    "pnl_pct": pnl_pct,
                    "reason": f"跟踪止损触发（最高 {p.highest_price:.6f} → 当前 {current_price:.6f}，回撤 {trailing_drawdown:.1f}%）"
                }
        
        # 止损
        if pnl_pct <= -abs(p.stop_loss_pct):
            return {
                "action": "STOP_LOSS",
                "pnl_sol": pnl_sol,
                "pnl_pct": pnl_pct,
                "reason": f"止损触发（-{abs(p.stop_loss_pct):.0f}%）"
            }
        
        # 止盈
        if pnl_pct >= p.take_profit_pct:
            return {
                "action": "TAKE_PROFIT",
                "pnl_sol": pnl_sol,
                "pnl_pct": pnl_pct,
                "reason": f"止盈触发（+{p.take_profit_pct:.0f}%）"
            }
        
        # 毕业信号（bonding = 100%）
        if bonding_pct >= 100:
            return {
                "action": "GRADUATED",
                "pnl_sol": pnl_sol,
                "pnl_pct": pnl_pct,
                "reason": f"代币毕业（Bonding 100%）"
            }
        
        return {"action": "NONE"}
    
    # ─── 统计 ─────────────────────────────────────────────────
    
    def get_daily_stats(self) -> dict:
        """今日统计"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        
        rows = cur.execute("""
            SELECT status, COUNT(*), SUM(pnl_sol), AVG(pnl_pct)
            FROM positions
            WHERE date(close_time) = ? AND status IN ('CLOSED','STOPPED','TAKEN_PROFIT')
            GROUP BY status
        """, (today,)).fetchall()
        
        conn.close()
        
        stats = {"trades": 0, "wins": 0, "losses": 0, "total_pnl_sol": 0}
        for status, count, pnl, avg_pnl in rows:
            stats["trades"] += count
            if pnl and pnl > 0:
                stats["wins"] += count
                stats["total_pnl_sol"] += pnl
            elif pnl and pnl < 0:
                stats["losses"] += count
                stats["total_pnl_sol"] += pnl
        
        return stats
    
    def get_total_pnl(self) -> float:
        """历史总 PnL"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        total = cur.execute(
            "SELECT COALESCE(SUM(pnl_sol), 0) FROM positions WHERE status != 'OPEN'"
        ).fetchone()[0]
        conn.close()
        return float(total)
