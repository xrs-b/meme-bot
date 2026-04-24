#!/usr/bin/env python3
"""
Telegram Alert Manager for Meme Bot
Handles all notifications to users via Telegram bot API
"""

import asyncio
import aiohttp
from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum

from .models import Signal, Trade, WalletPosition, Chain, SignalType, AlertMode


class AlertLevel(Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    DANGER = "danger"


class AlertManager:
    """Manages Telegram alerts for the Meme Bot"""
    
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}"
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def close(self):
        """Close the HTTP session"""
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        """Send a message to the configured Telegram chat"""
        if not self.bot_token or not self.chat_id:
            print(f"[Alert] Telegram not configured, skipping: {text[:100]}")
            return False
        
        try:
            session = await self._get_session()
            url = f"{self.api_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True
            }
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                result = await resp.json()
                if result.get("ok"):
                    return True
                else:
                    print(f"[Alert] Telegram error: {result.get('description')}")
                    return False
        except Exception as e:
            print(f"[Alert] Failed to send Telegram message: {e}")
            return False
    
    def _format_signal(self, signal: Signal) -> str:
        """Format a signal for Telegram message"""
        chain_emoji = "🟣" if signal.chain == Chain.SOLANA else "🟠"
        chain_name = "Solana" if signal.chain == Chain.SOLANA else "BSC"
        signal_emoji = {
            SignalType.NEW_COIN: "🆕",
            SignalType.LIQUIDITY_INCREASE: "💧",
            SignalType.VOLUME_SPIKE: "📈",
            SignalType.PRICE_PUMP: "🚀",
            SignalType.WALLET_ACTIVITY: "👛",
            SignalType.RUG_PULL: "⚠️",
            SignalType.NEW_POOL: "🏊"
        }.get(signal.type, "📢")
        
        # Score badge
        score_badge = "🟢" if signal.score >= 80 else "🟡" if signal.score >= 60 else "🔴"
        
        # Market cap (estimate from liquidity * multiplier)
        market_cap = signal.metadata.get('market_cap_usd', 0)
        if market_cap <= 0 and signal.price_at_signal > 0 and signal.metadata.get('total_supply', 0) > 0:
            market_cap = signal.price_at_signal * signal.metadata.get('total_supply', 0)
        
        # Volume / Liquidity ratio
        vol_liq_ratio = 0.0
        if signal.liquidity_at_signal > 0:
            vol_liq_ratio = signal.volume_24h_at_signal / signal.liquidity_at_signal
        
        # Links
        if signal.chain == Chain.SOLANA:
            explorer_link = f"https://solscan.io/token/{signal.token.address}"
            dex_link = f"https://dexscreener.com/solana/{signal.token.address}"
        else:
            explorer_link = f"https://bscscan.com/token/{signal.token.address}"
            dex_link = f"https://dexscreener.com/bsc/{signal.token.address}"
        
        message = f"""
{signal_emoji} *{signal.type.value.upper()} ALERT* {chain_emoji}

━━━━━━ *基本信息* ━━━━━━
🏷️ 代币名: *{signal.token.name}* (`{signal.token.symbol}`)
🔗 链: {chain_name} {chain_emoji}
📛 合约: `{signal.token.address}`
🌐 浏览器: [查看]({explorer_link}) | [DexScreener]({dex_link})

━━━━━━ *信号评分* ━━━━━━
{score_badge} 评分: *{signal.score}/100* ({self._score_label(signal.score)})
📊 置信度: {signal.confidence:.0%}
📝 {signal.message}

━━━━━━ *行情数据* ━━━━━━
💰 当前价格: `${signal.price_at_signal:,.8f}`
💵 市值(估算): `${self._format_usd(market_cap)}`
💧 流动性: `${self._format_usd(signal.liquidity_at_signal)}`
📊 24h 交易量: `${self._format_usd(signal.volume_24h_at_signal)}`
📈 Vol/Liq 比: {vol_liq_ratio:.2f}x

━━━━━━ *交易建议* ━━━━━━
💎 建议: {self._get_trading_suggestion(signal)}

🕐 发现时间: {signal.detected_at.strftime('%Y-%m-%d %H:%M:%S')} UTC
"""
        return message.strip()
    
    def _score_label(self, score: int) -> str:
        if score >= 80:
            return "⭐ 强烈推荐"
        elif score >= 70:
            return "🟢 良好"
        elif score >= 60:
            return "🟡 一般"
        elif score >= 40:
            return "🟠 观望"
        else:
            return "🔴 忽略"
    
    def _format_usd(self, value: float) -> str:
        if value >= 1_000_000_000:
            return f"${value / 1_000_000_000:.2f}B"
        elif value >= 1_000_000:
            return f"${value / 1_000_000:.2f}M"
        elif value >= 1_000:
            return f"${value / 1_000:.2f}K"
        else:
            return f"${value:.2f}"
    
    def _get_trading_suggestion(self, signal: Signal) -> str:
        """Get trading suggestion based on signal"""
        if signal.score >= 80:
            if signal.liquidity_at_signal >= 50000 and signal.volume_24h_at_signal >= 10000:
                return "✅ 机会极好，可考虑小额埋伏"
            elif signal.liquidity_at_signal >= 10000:
                return "✅ 机会良好，轻仓试探"
            else:
                return "⚠️ 流动性偏低，控制仓位"
        elif signal.score >= 60:
            if signal.liquidity_at_signal >= 5000:
                return "🟡 机会一般，轻仓观察"
            else:
                return "⚠️ 流动性不足，谨慎"
        else:
            return "🔴 评分过低，不建议操作"
    
    def _format_trade(self, trade: Trade, mode: AlertMode) -> str:
        """Format a trade for Telegram message"""
        chain_emoji = "🟣" if trade.chain == Chain.SOLANA else "🟠"
        action_emoji = "🟢" if trade.action.value == "buy" else "🔴"
        
        pnl_text = ""
        if trade.pnl_percent is not None:
            pnl_emoji = "📈" if trade.pnl_percent >= 0 else "📉"
            pnl_text = f"\n{pnl_emoji} P&L: `{trade.pnl_percent:+.2f}%` (`{trade.pnl_usd:+.2f} USD`)"
        
        auto_text = "🤖 *AUTO-EXECUTED*" if mode == AlertMode.AUTO_TRADE else "👀 *OBSERVATION*"
        
        message = f"""
{action_emoji} *{trade.action.value.upper()} EXECUTED* {chain_emoji}

🏷️ *{trade.token_symbol}*
💵 Amount: `{trade.amount_in:.4f}` | Received: `{trade.amount_out:.2f}`
💰 Price: `${trade.price:,.6f}`
💵 Value: `${trade.value_usd:,.2f}`

📋 模式: {auto_text}

🔗 [Tx](https://solscan.io/tx/{trade.tx_hash}) | 🕐 {trade.executed_at.strftime('%H:%M:%S')}
""".strip()
        
        return message
    
    async def alert_signal(self, signal: Signal):
        """Send signal alert to Telegram"""
        text = self._format_signal(signal)
        await self.send_message(text)
    
    async def alert_trade(self, trade: Trade, mode: AlertMode):
        """Send trade alert to Telegram"""
        text = self._format_trade(trade, mode)
        await self.send_message(text)
    
    async def alert_rug_pull(self, signal: Signal):
        """Send rug pull warning"""
        chain_emoji = "🟣" if signal.chain == Chain.SOLANA else "🟠"
        text = f"""
🚨 *RUG PULL WARNING* {chain_emoji}

🏷️ *{signal.token.name}* (`{signal.token.symbol}`)
📛 `{signal.token.address}`

⚠️ *{signal.message}*

💸流动性池已被大幅抽离！
建议立即检查持仓。
"""
        await self.send_message(text)
    
    async def alert_copy_trade(self, wallet: str, trade: Trade):
        """Send copy trade notification"""
        chain_emoji = "🟣" if trade.chain == Chain.SOLANA else "🟠"
        text = f"""
👛 *COPY TRADE ALERT* {chain_emoji}

� Wallet: `{wallet[:8]}...{wallet[-4:]}`

🟢 *BUY* detected:
🏷️ *{trade.token_symbol}*
💵 Amount: `{trade.amount_in:.4f}`
🔗 Following at {50}% size
"""
        await self.send_message(text)
    
    async def alert_positions_update(self, positions: List[WalletPosition], chain: Chain):
        """Send positions summary"""
        chain_emoji = "🟣" if chain == Chain.SOLANA else "🟠"
        
        if not positions:
            return
        
        lines = [f"📊 *POSITIONS UPDATE* {chain_emoji}\n"]
        
        total_value = 0
        total_pnl = 0
        
        for pos in positions[:10]:  # Top 10
            pnl_emoji = "📈" if pos.unrealized_pnl_percent >= 0 else "📉"
            lines.append(
                f"{pnl_emoji} `{pos.token_symbol}`: ${pos.value_usd:,.2f} "
                f"({pos.unrealized_pnl_percent:+.1f}%)"
            )
            total_value += pos.value_usd
            total_pnl += pos.unrealized_pnl_usd
        
        lines.append(f"\n💼 Total: ${total_value:,.2f} | P&L: ${total_pnl:,.2f}")
        
        await self.send_message("\n".join(lines))
    
    async def alert_stats(self, stats: Dict[str, Any], chain: Chain):
        """Send stats summary"""
        chain_emoji = "🟣" if chain == Chain.SOLANA else "🟠"
        text = f"""
📈 *STATS SUMMARY* {chain_emoji}

🪙 Signals: {stats.get('signal_count', 0)}
💱 Trades: {stats.get('trade_count', 0)}
💰 Total P&L: `${stats.get('total_pnl', 0):+.2f}`
📊 Win Rate: {stats.get('win_rate', 0):.1f}%
"""
        await self.send_message(text)
    
    async def alert_bot_status(self, status: str, chain: Optional[Chain] = None):
        """Send bot status update"""
        chain_emoji = ""
        if chain:
            chain_emoji = "🟣" if chain == Chain.SOLANA else "🟠"
        text = f"🤖 *BOT STATUS* {chain_emoji}\n\n{status}"
        await self.send_message(text)
    
    async def alert_test(self) -> bool:
        """Send test message to verify configuration"""
        text = "✅ *Meme Bot Connected!*\n\nYour Telegram alerts are working correctly."
        return await self.send_message(text)


class AlertManagerSync:
    """Synchronous wrapper for AlertManager (for non-async contexts)"""
    
    def __init__(self, alert_manager: AlertManager):
        self.manager = alert_manager
    
    def alert_signal(self, signal: Signal):
        """Send signal alert (sync)"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self.manager.alert_signal(signal))
            else:
                loop.run_until_complete(self.manager.alert_signal(signal))
        except RuntimeError:
            asyncio.run(self.manager.alert_signal(signal))
    
    def alert_trade(self, trade: Trade, mode: AlertMode):
        """Send trade alert (sync)"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self.manager.alert_trade(trade, mode))
            else:
                loop.run_until_complete(self.manager.alert_trade(trade, mode))
        except RuntimeError:
            asyncio.run(self.manager.alert_trade(trade, mode))
    
    def alert_rug_pull(self, signal: Signal):
        """Send rug pull alert (sync)"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self.manager.alert_rug_pull(signal))
            else:
                loop.run_until_complete(self.manager.alert_rug_pull(signal))
        except RuntimeError:
            asyncio.run(self.manager.alert_rug_pull(signal))
    
    def alert_test(self) -> bool:
        """Send test message (sync)"""
        try:
            return asyncio.run(self.manager.alert_test())
        except:
            return False
