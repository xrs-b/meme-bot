#!/usr/bin/env python3
"""
Meme Bot Trading Module - 主入口
运行方式：
    python trading_main.py

职责：
    1. 加载配置和仓位状态
    2. 启动 Telegram Listener（监听指令 + 确认按钮）
    3. 启动仓位监控循环（定期检查止盈/止损/毕业）

与 scan_worker2.py 的关系：
    scan_worker2.py 推送信号到 Telegram
    trading_main.py 监听 Telegram Bot 收到的信号/指令并执行交易
    两者独立运行，互不干扰
"""
import asyncio
import json
import logging
import sys
import os

# 加载项目路径
sys.path.insert(0, '/root/.openclaw/workspace/meme-bot')

from trading.config_manager import ConfigManager, BotMode
from trading.position_manager import PositionManager
from trading.swap_executor import SwapExecutor
from trading.decision_engine import DecisionEngine
from trading.telegram_listener import TelegramListener

# ─── 日志配置 ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("/tmp/trading_main.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ─── 全局实例 ───────────────────────────────────────────────
CONFIG_PATH = "/root/.openclaw/workspace/meme-bot/config.json"
BOT_TOKEN = "8731393023:AAFWqm4acHgMmocuGA8gEehk8BEtK3S8L-c"  # 从 notifier 复用
WALLET_ADDRESS = "DHuirUA2btsoFDpLHofjgZs9EE73tPVEB6Bcsm1gEd47"  # OKX 钱包

# ─── 全局单例 ───────────────────────────────────────────────
config_mgr = ConfigManager(CONFIG_PATH)
pm = PositionManager()
executor = SwapExecutor(WALLET_ADDRESS)
de = DecisionEngine(config_mgr, pm, executor)


async def position_monitor_loop():
    """
    仓位监控循环
    每 15 秒检查一次所有持仓的止盈/止损/跟踪止损/毕业
    """
    import subprocess
    
    logger.info("Position monitor loop started")
    
    while True:
        try:
            positions = pm.get_all_open()
            
            for p in positions:
                # 获取当前价格
                price_data = _get_token_price(p.token_address)
                if not price_data:
                    continue
                
                current_price = price_data.get("price_usd", 0)
                bonding = price_data.get("bonding_pct", 0)
                
                # 更新最高价
                if current_price > p.highest_price:
                    pm.update_highest_price(p.id, current_price)
                
                # 检查止盈止损
                result = pm.check_conditions(p.token_address, current_price, bonding)
                action = result["action"]
                
                if action == "NONE":
                    continue
                
                logger.info(f"Position {p.symbol}: {action} triggered | PnL: {result['pnl_pct']:+.1f}%")
                
                # 止损 → 立即执行，不等确认
                if action == "STOP_LOSS":
                    slip = config_mgr.config.slippage_migrating_pct
                    exec_result = await executor.sell_token(p.token_address, p.quantity, slippage_pct=slip)
                    if exec_result.success:
                        pm.close_position(p.id, exec_result.to_amount,
                                        reason="STOPPED",
                                        pnl_sol=result["pnl_sol"],
                                        pnl_pct=result["pnl_pct"])
                        de.on_trade_result(result["pnl_sol"], was_successful=False)
                        _notify_telegram(f"🛑 止损触发\n{p.symbol}: {result['pnl_pct']:+.1f}%\nTX: {exec_result.tx_hash}")
                    else:
                        logger.error(f"Stop loss failed: {exec_result.error}")
                
                # 止盈 → 自动执行卖出
                elif action == "TAKE_PROFIT":
                    slip = config_mgr.config.slippage_migrating_pct
                    exec_result = await executor.sell_token(p.token_address, p.quantity, slippage_pct=slip)
                    if exec_result.success:
                        pm.close_position(p.id, exec_result.to_amount,
                                        reason="TAKE_PROFIT",
                                        pnl_sol=result["pnl_sol"],
                                        pnl_pct=result["pnl_pct"])
                        de.on_trade_result(result["pnl_sol"], was_successful=(result["pnl_sol"] >= 0))
                        _notify_telegram(f"🎯 止盈自动卖出\n{p.symbol}: {result['pnl_pct']:+.1f}%\n获得: {exec_result.to_amount:.4f} SOL\nTX: {exec_result.tx_hash}")
                    else:
                        logger.error(f"Take profit failed: {exec_result.error}")
                
                # 毕业 → 自动执行卖出
                elif action == "GRADUATED":
                    slip = config_mgr.config.slippage_migrating_pct
                    exec_result = await executor.sell_token(p.token_address, p.quantity, slippage_pct=slip)
                    if exec_result.success:
                        pm.close_position(p.id, exec_result.to_amount,
                                        reason="GRADUATED",
                                        pnl_sol=result["pnl_sol"],
                                        pnl_pct=result["pnl_pct"])
                        de.on_trade_result(result["pnl_sol"], was_successful=(result["pnl_sol"] >= 0))
                        _notify_telegram(f"🎓 毕业自动卖出\n{p.symbol}: {result['pnl_pct']:+.1f}%\n获得: {exec_result.to_amount:.4f} SOL\nTX: {exec_result.tx_hash}")
                    else:
                        logger.error(f"Graduated sell failed: {exec_result.error}")
            
            # 清理过期忽略记录
            de.cleanup_ignored()
            
        except Exception as e:
            logger.error(f"Monitor loop error: {e}")
        
        await asyncio.sleep(10)


def _get_token_price(token_address: str) -> dict:
    """获取代币当前价格"""
    import subprocess, json
    env = os.environ.copy()
    env["PATH"] = "/root/.local/bin:" + env.get("PATH", "")
    result = subprocess.run(
        f'onchainos token price-info --chain solana --address {token_address}',
        shell=True, capture_output=True, text=True, env=env, timeout=20
    )
    try:
        data = json.loads(result.stdout)
        pdata = (data.get("data") or [{}])[0]
        return {
            "price_usd": float(pdata.get("price", 0) or 0),
            "bonding_pct": 0  # price-info 没有 bonding，需要从 memepump tokens 获取
        }
    except:
        return {}


def _notify_telegram(message: str, markup: dict = None):
    """通过 Telegram Bot 发送通知（异步，不阻塞）"""
    asyncio.create_task(_aio_post(message, markup))

async def _aio_post(message: str, markup: dict = None):
    import httpx
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    params = {
        "chat_id": "6941139355",
        "text": message,
    }
    if markup:
        params["reply_markup"] = json.dumps(markup)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            await client.post(url, json=params)
    except Exception as e:
        logger.warning(f"Telegram notify failed: {e}")


async def main():
    logger.info("=" * 50)
    logger.info("Meme Bot Trading Module Started")
    logger.info(f"Mode: {config_mgr.mode.value}")
    logger.info(f"Wallet: {WALLET_ADDRESS}")
    logger.info("=" * 50)
    
    listener = TelegramListener(
        bot_token=BOT_TOKEN,
        config_manager=config_mgr,
        position_manager=pm,
        swap_executor=executor,
        decision_engine=de
    )
    
    # 并行运行 listener + monitor
    await asyncio.gather(
        listener.start_listening(),
        position_monitor_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
