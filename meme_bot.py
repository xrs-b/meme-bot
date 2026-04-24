#!/usr/bin/env python3
"""
Meme Bot - Main Entry Point
Cross-chain Meme Coin Monitor & Trading Bot
"""

import asyncio
import argparse
import logging
import signal
import sys
from pathlib import Path
from datetime import datetime, timedelta

from core.models import Chain, BotConfig, AlertMode, Signal, Position
from core.database import Database
from core.alert_manager import AlertManager
from core.signal_detector import SignalDetector
from core.trading_engine import TradingEngine
from core.copy_trader import CopyTrader

from solana.adapter import SolanaAdapter
from bsc.adapter import BSCAdapter

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class MemeBot:
    """
    Main Meme Bot class.
    Orchestrates all components for cross-chain meme coin trading.
    """
    
    def __init__(self, config: BotConfig):
        self.config = config
        
        # Database
        self.db = Database("meme_bot.db", ttl_days=config.data_ttl_days)
        
        # Alert Manager
        self.alert_manager = AlertManager(
            config.telegram_bot_token,
            config.telegram_chat_id
        )
        
        # Signal Detector
        self.signal_detector = SignalDetector(
            self.db,
            self.alert_manager,
            config
        )
        
        # Trading Engine
        self.trading_engine = TradingEngine(
            self.db,
            self.alert_manager,
            config
        )
        
        # Copy Trader
        self.copy_trader = CopyTrader(
            self.db,
            self.alert_manager,
            config
        )
        
        # Risk Manager
        from core.risk_manager import RiskManager
        self.risk_manager = RiskManager(
            self.db,
            self.alert_manager,
            config
        )
        
        # Chain Adapters
        self._adapters = {}
        
        # Running state
        self._running = False
    
    async def start(self):
        """Start the Meme Bot"""
        logger.info("Starting Meme Bot...")
        self._running = True
        
        # Initialize adapters
        await self._init_adapters()
        
        # Set up signal callbacks
        self.signal_detector.add_callback(self._on_signal)
        
        # Set executor references
        for chain, adapter in self._adapters.items():
            self.trading_engine.set_executor(chain, adapter)
            self.copy_trader.set_tracker(chain, adapter)
        
        self.copy_trader.set_trading_engine(self.trading_engine)
        
        # Start all components
        await self.signal_detector.start()
        await self.copy_trader.start()
        await self.risk_manager.start()
        
        for adapter in self._adapters.values():
            await adapter.start()
        
        # Send startup alert
        await self.alert_manager.alert_bot_status(
            f"✅ Meme Bot started!\n\n"
            f"🟣 SOL: {'Running' if Chain.SOLANA in self._adapters else 'Disabled'}\n"
            f"🟠 BSC: {'Running' if Chain.BSC in self._adapters else 'Disabled'}\n\n"
            f"Mode: {'🤖 Auto-trade' if self.config.alert_mode == AlertMode.AUTO_TRADE else '🔔 Notify only'}\n"
            f"Copy trading: {'✅ On' if self.copy_trader.enabled else '❌ Off'}"
        )
        
        logger.info("Meme Bot started successfully!")
        
        # Keep running
        while self._running:
            await asyncio.sleep(10)
    
    async def stop(self):
        """Stop the Meme Bot"""
        logger.info("Stopping Meme Bot...")
        self._running = False
        
        # Stop all components
        await self.signal_detector.stop()
        await self.copy_trader.stop()
        await self.risk_manager.stop()
        
        for adapter in self._adapters.values():
            await adapter.stop()
        
        await self.alert_manager.close()
        
        logger.info("Meme Bot stopped")
    
    async def _init_adapters(self):
        """Initialize chain adapters"""
        # Solana
        if self.config.solana_rpc:
            try:
                sol_adapter = SolanaAdapter(self.config, self.db)
                sol_adapter.set_callbacks(
                    on_new_pool=self._handle_new_pool,
                    on_wallet_activity=self._handle_wallet_activity
                )
                self._adapters[Chain.SOLANA] = sol_adapter
                logger.info("Solana adapter initialized")
            except Exception as e:
                logger.error(f"Failed to init Solana: {e}")
        
        # BSC
        if self.config.bsc_rpc:
            try:
                bsc_adapter = BSCAdapter(self.config, self.db)
                bsc_adapter.set_callbacks(
                    on_new_pool=self._handle_new_pool,
                    on_wallet_activity=self._handle_wallet_activity
                )
                self._adapters[Chain.BSC] = bsc_adapter
                logger.info("BSC adapter initialized")
            except Exception as e:
                logger.error(f"Failed to init BSC: {e}")
    
    async def _handle_new_pool(self, pool_address: str, token, pool_data: Dict):
        """Handle new pool detected by adapter"""
        signal = await self.signal_detector.check_new_pool(pool_address, token, pool_data)
        if signal:
            logger.info(f"New pool detected: {token.symbol} on {token.chain.value}")
    
    def _handle_wallet_activity(self, **kwargs):
        """Handle wallet activity from adapters"""
        self.copy_trader.on_wallet_activity(**kwargs)
    
    async def _on_signal(self, signal: Signal):
        """Handle detected signal"""
        logger.info(f"Signal: {signal.type.value} for {signal.token.symbol} (score: {signal.score})")
        
        # Alert immediately (always, even in observation mode)
        await self.alert_manager.alert_signal(signal)
        
        # Auto-trade only if in AUTO_TRADE mode (not observation mode)
        if self.config.alert_mode == AlertMode.AUTO_TRADE:
            if signal.score >= self.config.min_signal_score:
                # Check risk rules before trading
                risk_check = self.risk_manager.check_signal_risk(signal)
                
                if risk_check['approved']:
                    # Check position limits
                    if self.risk_manager.check_total_exposure(
                        self.config.default_amount_per_trade,
                        signal.chain
                    )['approved']:
                        await self.trading_engine.execute_buy(signal)
                    else:
                        logger.info(f"Position limit reached, skipping trade")
                else:
                    logger.info(f"Risk check failed: {risk_check['reasons']}")
    
    # ============ TELEGRAM COMMANDS ============
    
    async def cmd_status(self) -> str:
        """Get bot status"""
        lines = ["📊 *Bot Status*\n"]
        
        for chain in [Chain.SOLANA, Chain.BSC]:
            if chain in self._adapters:
                stats = self.signal_detector.get_signal_stats(chain)
                positions = self.trading_engine.get_positions(chain)
                
                chain_emoji = "🟣" if chain == Chain.SOLANA else "🟠"
                lines.append(f"{chain_emoji} *{chain.value.upper()}*")
                lines.append(f"  Signals: {stats.get('signal_count', 0)}")
                lines.append(f"  Trades: {stats.get('trade_count', 0)}")
                lines.append(f"  Positions: {len(positions)}")
                lines.append("")
        
        lines.append(f"Mode: {'🤖 Auto' if self.config.alert_mode == AlertMode.AUTO_TRADE else '🔔 Notify'}")
        lines.append(f"Copy trading: {'✅' if self.copy_trader.enabled else '❌'}")
        
        return "\n".join(lines)
    
    async def cmd_positions(self, chain: Chain) -> str:
        """Get positions for a chain"""
        positions = self.trading_engine.get_positions(chain)
        
        if not positions:
            return f"No positions on {chain.value.upper()}"
        
        chain_emoji = "🟣" if chain == Chain.SOLANA else "🟠"
        lines = [f"{chain_emoji} *{chain.value.upper()} Positions*\n"]
        
        total_value = 0
        total_pnl = 0
        
        for pos in positions:
            pnl_emoji = "📈" if pos.pnl_percent >= 0 else "📉"
            lines.append(
                f"{pnl_emoji} `{pos.token_symbol}`\n"
                f"   Qty: {pos.quantity:.2f} | Value: ${pos.value_now:.2f}\n"
                f"   P&L: {pos.pnl_percent:+.2f}% (${pos.pnl_usd:+.2f})"
            )
            total_value += pos.value_now
            total_pnl += pos.pnl_usd
        
        lines.append(f"\n💼 Total: ${total_value:.2f} | P&L: ${total_pnl:+.2f}")
        
        return "\n".join(lines)
    
    async def cmd_liquidate(self, chain: Chain, token_address: Optional[str] = None) -> str:
        """Liquidate positions"""
        if token_address:
            trade = await self.trading_engine.liquidate_position(token_address, chain)
            if trade:
                return f"✅ Liquidated {trade.token_symbol}: {trade.pnl_percent:+.2f}%"
            return "❌ Position not found or liquidation failed"
        else:
            trades = await self.trading_engine.liquidate_all(chain)
            if trades:
                total_pnl = sum(t.pnl_percent for t in trades) / len(trades)
                return f"✅ Liquidated {len(trades)} positions, avg P&L: {total_pnl:+.2f}%"
            return "❌ No positions to liquidate"
    
    async def cmd_signals(self, chain: Chain, hours: int = 24) -> str:
        """Get recent signals"""
        signals = self.signal_detector.get_recent_signals(chain, hours)
        
        if not signals:
            return f"No signals in the last {hours}h on {chain.value.upper()}"
        
        chain_emoji = "🟣" if chain == Chain.SOLANA else "🟠"
        lines = [f"{chain_emoji} *Recent Signals on {chain.value.upper()}*\n"]
        
        for sig in signals[:10]:
            score_emoji = "🟢" if sig.score >= 80 else "🟡" if sig.score >= 60 else "🔴"
            lines.append(
                f"{score_emoji} [{sig.score}] {sig.token.symbol}\n"
                f"   {sig.type.value} | ${sig.liquidity_at_signal:,.0f} liq"
            )
        
        return "\n".join(lines)
    
    async def cmd_stats(self, chain: Chain) -> str:
        """Get stats"""
        stats = self.signal_detector.get_signal_stats(chain)
        chain_emoji = "🟣" if chain == Chain.SOLANA else "🟠"
        
        return f"""{chain_emoji} *{chain.value.upper()} Stats*

🪙 Signals: {stats.get('signal_count', 0)}
💱 Trades: {stats.get('trade_count', 0)}
💰 Total P&L: ${stats.get('total_pnl', 0):+.2f}
📊 Win Rate: {stats.get('win_rate', 0):.1f}%
"""
    
    async def cmd_set_mode(self, mode: AlertMode) -> str:
        """Set trading mode"""
        self.config.alert_mode = mode
        self.trading_engine.set_mode(mode)
        
        mode_str = "🤖 Auto-trade" if mode == AlertMode.AUTO_TRADE else "🔔 Notify only"
        return f"✅ Mode set to {mode_str}"
    
    async def cmd_add_wallet(self, address: str, chain: Chain, label: str = "") -> str:
        """Add wallet to copy trade"""
        self.copy_trader.add_wallet(address, chain, label)
        return f"✅ Added wallet `{address[:8]}...` to {chain.value} copy list"
    
    async def cmd_copy_trading(self, enabled: bool) -> str:
        """Toggle copy trading"""
        if enabled:
            self.copy_trader.enable()
            return "✅ Copy trading enabled"
        else:
            self.copy_trader.disable()
            return "❌ Copy trading disabled"
    
    async def cmd_help(self) -> str:
        """Get help message"""
        return """
🤖 *Meme Bot Commands*

📊 *Status*
`/status` - Bot status
`/positions [sol|bsc]` - Show positions
`/signals [sol|bsc] [hours]` - Recent signals
`/stats [sol|bsc]` - Trading stats

💰 *Trading*
`/buy <symbol> <amount>` - Buy token
`/sell <symbol> [qty]` - Sell token
`/liquidate [sol|bsc]` - Liquidate all
`/liquidate <token>` - Liquidate single

⚙️ *Settings*
`/mode [notify|auto]` - Set mode
`/copy [on|off]` - Toggle copy trading
`/addwallet <addr> [sol|bsc]` - Add wallet to copy

🔧 *System*
`/help` - This message
`/test` - Test Telegram
"""


# Global bot instance for signal handlers
_bot: Optional[MemeBot] = None


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Meme Bot - Cross-chain Meme Coin Trading")
    parser.add_argument("--config", type=str, default="config.json", help="Config file path")
    parser.add_argument("--web", action="store_true", help="Start web dashboard")
    parser.add_argument("--web-port", type=int, default=8080, help="Web dashboard port")
    parser.add_argument("--web-host", type=str, default="0.0.0.0", help="Web dashboard host")
    parser.add_argument("--observe", action="store_true", help="Run in observation mode (no real trading)")
    parser.add_argument("--backtest", action="store_true", help="Run backtest and exit")
    parser.add_argument("--backtest-days", type=int, default=7, help="Backtest days")
    parser.add_argument("--backtest-chain", type=str, default="all", choices=["sol", "bsc", "all"], help="Chain to backtest")
    args = parser.parse_args()
    
    # Load config
    config_path = Path(args.config)
    if config_path.exists():
        import json
        with open(config_path) as f:
            config_data = json.load(f)
        
        config = BotConfig(**config_data)
    else:
        # Use defaults / env vars
        import os
        config = BotConfig(
            solana_rpc=os.getenv("SOLANA_RPC", ""),
            bsc_rpc=os.getenv("BSC_RPC", ""),
            wallet_address=os.getenv("WALLET_ADDRESS", ""),
            wallet_private_key=os.getenv("WALLET_PRIVATE_KEY", ""),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            alert_mode=AlertMode.NOTIFY_ONLY,
            copy_trade_enabled=False
        )
    
    # Observation mode - force notify only, no private key needed
    if args.observe:
        config.alert_mode = AlertMode.NOTIFY_ONLY
        config.wallet_private_key = ""
        logger.info("Running in OBSERVATION MODE - no real trades will be executed")
    
    global _bot
    _bot = MemeBot(config)
    
    # Backtest mode
    if args.backtest:
        from core.backtester import Backtester
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        bt = Backtester(_bot.db, config)
        chain = None
        if args.backtest_chain == 'sol':
            chain = Chain.SOLANA
        elif args.backtest_chain == 'bsc':
            chain = Chain.BSC
        
        result = loop.run_until_complete(bt.run_backtest(
            chain=chain,
            start_date=datetime.utcnow() - timedelta(days=args.backtest_days),
            end_date=datetime.utcnow(),
            min_score=60
        ))
        
        print(bt.format_backtest_report(result))
        loop.close()
        return
    
    # Web dashboard mode
    if args.web:
        from web.dashboard import WebDashboard, create_dashboard_template
        create_dashboard_template()
        
        dashboard = WebDashboard(_bot, host=args.web_host, port=args.web_port)
        dashboard.start()
        
        logger.info(f"Web dashboard running at http://{args.web_host}:{args.web_port}")
        
        # Run bot with dashboard
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def run_with_dashboard():
            # Start bot
            bot_task = asyncio.create_task(_bot.start())
            # Keep running
            while _bot._running:
                await asyncio.sleep(10)
            await _bot.stop()
        
        async def shutdown():
            _bot._running = False
            dashboard.stop()
            await _bot.stop()
        
        try:
            loop.run_until_complete(run_with_dashboard())
        except KeyboardInterrupt:
            loop.run_until_complete(shutdown())
        finally:
            loop.close()
        return
    
    # Normal mode
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def run():
        await _bot.start()
    
    async def shutdown():
        await _bot.stop()
    
    try:
        loop.run_until_complete(run())
    except KeyboardInterrupt:
        loop.run_until_complete(shutdown())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
