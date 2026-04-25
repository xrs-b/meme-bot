"""
Telegram 指令监听器
监听 Bot 消息，解析命令和按钮回调
同时负责交易确认流程（半自动模式）
"""
import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional
from .config_manager import BotMode

logger = logging.getLogger(__name__)

# 已知的确认中请求（等待用户点击）
_pending_confirms: dict[str, dict] = {}


class TelegramListener:
    """
    监听 Telegram Bot 指令和按钮回调
    通过轮询 Telegram Bot API 获取更新
    """
    
    def __init__(self, bot_token: str, config_manager,
                 position_manager, swap_executor, decision_engine):
        self.bot_token = bot_token
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.offset = None
        self.config = config_manager
        self.pm = position_manager
        self.executor = swap_executor
        self.de = decision_engine
    
    async def _call(self, method: str, **params) -> dict:
        import aiohttp
        url = f"{self.base_url}/{method}"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return await resp.json()
    
    async def _send_message(self, chat_id: int, text: str,
                           reply_markup: Optional[dict] = None,
                           parse_mode: str = "Markdown") -> int:
        """发送消息，返回 message_id"""
        result = await self._call("sendMessage", 
            chat_id=chat_id, text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )
        return result.get("result", {}).get("message_id", 0)
    
    def _make_buttons(self, actions: list[tuple[str, str]]) -> dict:
        """
        构建 InlineKeyboardMarkup
        actions: [(label, callback_data), ...]
        """
        buttons = [[{"text": label, "callback_data": data}] for label, data in actions]
        return {"inline_keyboard": buttons}
    
    async def start_listening(self):
        """开始轮询监听（阻塞循环）"""
        logger.info("Telegram listener started")
        while True:
            try:
                updates = await self._get_updates()
                for update in updates:
                    await self._handle_update(update)
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Telegram listener error: {e}")
                await asyncio.sleep(5)
    
    async def _get_updates(self) -> list:
        params = {"timeout": 30}
        if self.offset:
            params["offset"] = self.offset
        try:
            result = await self._call("getUpdates", **params)
            updates = result.get("result", [])
            if updates:
                self.offset = max(u["update_id"] for u in updates) + 1
            return updates
        except Exception as e:
            logger.warning(f"getUpdates failed: {e}")
            return []
    
    async def _handle_update(self, update: dict):
        """处理单条更新"""
        msg = update.get("message") or update.get("callback_query", {}).get("message")
        if not msg:
            return
        
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "")
        callback_query = update.get("callback_query", {})
        callback_data = callback_query.get("data", "")
        
        if callback_data:
            await self._handle_callback(chat_id, callback_data, msg)
        elif text:
            await self._handle_command(chat_id, text, msg)
    
    async def _handle_callback(self, chat_id: int, data: str, msg: dict):
        """处理按钮回调"""
        parts = data.split(":", 1)
        action = parts[0]
        payload = parts[1] if len(parts) > 1 else ""
        
        if action == "CONFIRM_BUY":
            await self._confirm_buy(chat_id, payload, msg)
        elif action == "REJECT":
            await self._reject(chat_id, payload, msg)
        elif action == "CONFIRM_SELL":
            await self._confirm_sell(chat_id, payload, msg)
        elif action == "IGNORE_TOKEN":
            await self._ignore_token(chat_id, payload, msg)
    
    async def _handle_command(self, chat_id: int, text: str, msg: dict):
        """处理文本命令"""
        cmd = text.strip().split()[0].lower()
        
        if cmd == "/start":
            await self._cmd_start(chat_id)
        elif cmd == "/status":
            await self._cmd_status(chat_id)
        elif cmd == "/positions":
            await self._cmd_positions(chat_id)
        elif cmd == "/profit":
            await self._cmd_profit(chat_id)
        elif cmd == "/pause":
            await self._cmd_pause(chat_id)
        elif cmd == "/resume":
            await self._cmd_resume(chat_id)
        elif cmd == "/mode":
            await self._cmd_mode(chat_id, text)
        elif cmd == "/buy":
            await self._cmd_manual_buy(chat_id, text)
        elif cmd == "/close":
            await self._cmd_close(chat_id, text)
        elif cmd == "/help":
            await self._cmd_help(chat_id)
    
    # ─── 确认流程 ─────────────────────────────────────────────
    
    async def send_buy_confirmation(self, chat_id: int, token: dict, score: int,
                                    amount_sol: float, stop_loss: float,
                                    take_profit: float, position_id: str):
        """
        发送买入确认消息（半自动流程）
        """
        stage = token.get("stage", "NEW")
        symbol = token.get("symbol", "?")
        name = token.get("name", "?")
        address = token.get("address", "?")
        
        slippage = self.config.config.slippage_new_pct if stage == "NEW" else self.config.config.slippage_migrating_pct
        
        text = f"""📩 **半自动交易确认**

🏷️ {name}（{symbol}）
📛 合约: `{address}`

📊 评分: {score}/100
📍 阶段: {stage}
💰 建议仓位: {amount_sol:.3f} SOL
🛡️ 止损: -{stop_loss:.0f}%
🎯 止盈: +{take_profit:.0f}%
💹 滑点容忍: {slippage:.0f}%

点击确认后将在 30 秒内执行买入。"""
        
        markup = self._make_buttons([
            ("✅ 确认买入", f"CONFIRM_BUY:{position_id}"),
            ("❌ 拒绝", f"REJECT:{position_id}"),
            ("🚫 忽略此币", f"IGNORE_TOKEN:{address}"),
        ])
        
        msg_id = await self._send_message(chat_id, text, markup)
        
        # 记录等待确认的请求（5分钟过期）
        _pending_confirms[position_id] = {
            "chat_id": chat_id,
            "msg_id": msg_id,
            "token": token,
            "amount_sol": amount_sol,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "sent_at": datetime.now(timezone.utc).timestamp()
        }
        
        # 5 分钟后自动过期
        asyncio.create_task(self._expire_confirm(position_id, 300))
    
    async def _expire_confirm(self, position_id: str, seconds: int):
        await asyncio.sleep(seconds)
        if position_id in _pending_confirms:
            del _pending_confirms[position_id]
            logger.info(f"Confirm request {position_id} expired")
    
    async def _confirm_buy(self, chat_id: int, position_id: str, msg: dict):
        """用户点击了确认买入"""
        confirm = _pending_confirms.get(position_id)
        if not confirm:
            await self._send_message(chat_id, "⏰ 确认已过期，请重新触发信号")
            return
        
        del _pending_confirms[position_id]
        token = confirm["token"]
        amount_sol = confirm["amount_sol"]
        
        # 异步执行交易（不阻塞 listener）
        asyncio.create_task(self._execute_buy(chat_id, token, amount_sol))
    
    async def _execute_buy(self, chat_id: int, token: dict, amount_sol: float):
        """后台执行买入"""
        address = token["address"]
        stage = token.get("stage", "NEW")
        slippage = self.config.config.slippage_new_pct if stage == "NEW" else self.config.config.slippage_migrating_pct
        
        result = await self.executor.buy_token(address, amount_sol, slippage_pct=slippage)
        
        if result.success:
            await self._send_message(chat_id,
                f"✅ **买入成功！**\n\n"
                f"代币: {token.get('symbol')}\n"
                f"数量: {result.to_amount:.4f}\n"
                f"TX: `{result.tx_hash}`\n\n"
                f"⏰ 止盈止损监控已开启"
            )
        else:
            await self._send_message(chat_id,
                f"❌ **买入失败**\n\n{result.error}\n\n请检查钱包余额和滑点设置。"
            )
    
    async def _reject(self, chat_id: int, position_id: str, msg: dict):
        """拒绝交易"""
        if position_id in _pending_confirms:
            del _pending_confirms[position_id]
        await self._send_message(chat_id, "❌ 已拒绝本次交易。")
    
    async def _ignore_token(self, chat_id: int, address: str, msg: dict):
        """忽略该代币（未来1小时不再提示）"""
        self.de.ignore_token(address, ttl_seconds=3600)
        await self._send_message(chat_id, f"🚫 已忽略 `{address}`，1小时内不再推送。")
    
    async def _confirm_sell(self, chat_id: int, position_id: str, msg: dict):
        """用户确认卖出"""
        p = self.pm.get_position(position_id)
        if not p:
            await self._send_message(chat_id, "❌ 持仓不存在或已平仓")
            return
        
        # 执行卖出
        stage = p.stage
        slippage = self.config.config.slippage_migrating_pct
        result = await self.executor.sell_token(
            p.token_address, p.quantity, slippage_pct=slippage
        )
        
        if result.success:
            pnl = p.cost_sol * ((result.to_amount - p.cost_sol) / p.cost_sol)
            self.pm.close_position(position_id, result.to_amount,
                                  reason="TAKEN_PROFIT",
                                  pnl_sol=pnl, pnl_pct=(result.to_amount/p.cost_sol-1)*100)
            await self._send_message(chat_id,
                f"✅ **卖出成功！**\n\n"
                f"代币: {p.symbol}\n"
                f"卖出金额: {result.to_amount:.4f} SOL\n"
                f"盈亏: {pnl:+.4f} SOL\n"
                f"TX: `{result.tx_hash}`"
            )
        else:
            await self._send_message(chat_id, f"❌ 卖出失败：{result.error}")
    
    # ─── 命令处理 ─────────────────────────────────────────────
    
    async def _cmd_start(self, chat_id: int):
        mode = self.config.mode.value.replace("_", " ").title()
        await self._send_message(chat_id,
            f"🤖 **Meme Bot 交易助手**\n\n"
            f"当前模式: {mode}\n\n"
            f"/status - 查看当前仓位\n"
            f"/positions - 活跃持仓详情\n"
            f"/profit - 历史盈亏\n"
            f"/pause - 暂停自动交易\n"
            f"/resume - 恢复自动交易\n"
            f"/mode - 切换模式\n"
            f"/help - 帮助"
        )
    
    async def _cmd_status(self, chat_id: int):
        positions = self.pm.get_all_open()
        stats = self.pm.get_daily_stats()
        total_cost = self.pm.get_total_cost_sol()
        total_pnl = self.pm.get_total_pnl()
        mode = self.config.mode.value.replace("_", " ").title()
        
        status = f"""📊 **当前状态**

🤖 模式: {mode}
📍 活跃持仓: {len(positions)} 个
💰 仓位总额: {total_cost:.3f} SOL
📈 历史总盈亏: {total_pnl:+.4f} SOL

**今日**
🔄 交易笔数: {stats['trades']}
✅ 胜: {stats['wins']} | ❌ 负: {stats['losses']}
💰 今日盈亏: {stats['total_pnl_sol']:+.4f} SOL"""
        
        await self._send_message(chat_id, status)
    
    async def _cmd_positions(self, chat_id: int):
        positions = self.pm.get_all_open()
        if not positions:
            await self._send_message(chat_id, "📭 暂无活跃持仓")
            return
        
        lines = ["📍 **活跃持仓**\n"]
        for p in positions:
            lines.append(
                f"• {p.symbol} | 买入 {p.cost_sol:.3f} SOL\n"
                f"  评分 {p.score} | {p.stage}\n"
                f"  止盈 +{p.take_profit_pct:.0f}% | 止损 -{p.stop_loss_pct:.0f}%\n"
            )
        
        await self._send_message(chat_id, "\n".join(lines))
    
    async def _cmd_profit(self, chat_id: int):
        total = self.pm.get_total_pnl()
        stats = self.pm.get_daily_stats()
        await self._send_message(chat_id,
            f"📈 **盈亏统计**\n\n"
            f"💰 历史总盈亏: {total:+.4f} SOL\n"
            f"📅 今日盈亏: {stats['total_pnl_sol']:+.4f} SOL\n"
            f"🔄 今日交易: {stats['trades']} 笔"
        )
    
    async def _cmd_pause(self, chat_id: int):
        self.config.set_mode(BotMode.SIGNAL_ONLY)
        await self._send_message(chat_id,
            "⏸️ **已切换为信号模式**\n"
            "自动交易已暂停，持仓保留。\n"
            "/resume 恢复交易"
        )
    
    async def _cmd_resume(self, chat_id: int):
        self.config.set_mode(BotMode.SIGNAL_AND_TRADE)
        await self._send_message(chat_id,
            "▶️ **已恢复交易模式**\n"
            "将自动执行符合条件的交易。"
        )
    
    async def _cmd_mode(self, chat_id: int, text: str):
        parts = text.split()
        if len(parts) < 2:
            mode = self.config.mode.value
            await self._send_message(chat_id,
                f"当前模式: `{mode}`\n\n"
                f"/mode signal_only - 仅推送\n"
                f"/mode signal_and_trade - 推送+交易"
            )
            return
        
        new_mode = parts[1].lower()
        if new_mode == "signal_only":
            self.config.set_mode(BotMode.SIGNAL_ONLY)
            await self._send_message(chat_id, "✅ 已切换为：仅推送模式")
        elif new_mode == "signal_and_trade":
            self.config.set_mode(BotMode.SIGNAL_AND_TRADE)
            await self._send_message(chat_id, "✅ 已切换为：推送+交易模式")
        else:
            await self._send_message(chat_id, "❌ 未知模式")
    
    async def _cmd_manual_buy(self, chat_id: int, text: str):
        parts = text.split()
        if len(parts) < 2:
            await self._send_message(chat_id,
                "用法: /buy <合约地址> [SOL数量]\n"
                "例: /buy 合约地址 0.1"
            )
            return
        
        address = parts[1]
        amount = float(parts[2]) if len(parts) > 2 else 0.05
        
        asyncio.create_task(self._execute_buy(chat_id, {"address": address, "symbol": "?", "name": "Manual"}, amount))
        await self._send_message(chat_id, f"🔄 正在买入 {amount} SOL 的代币...")
    
    async def _cmd_close(self, chat_id: int, text: str):
        parts = text.split()
        if len(parts) < 2:
            await self._send_message(chat_id,
                "用法: /close <合约地址>\n"
                "例: /close 合约地址"
            )
            return
        
        address = parts[1]
        p = self.pm.get_position_by_token(address)
        if not p:
            await self._send_message(chat_id, "❌ 未找到该代币的活跃持仓")
            return
        
        slippage = self.config.config.slippage_migrating_pct
        result = await self.executor.sell_token(address, p.quantity, slippage_pct=slippage)
        
        if result.success:
            pnl = result.to_amount - p.cost_sol
            self.pm.close_position(p.id, result.to_amount,
                                  reason="MANUAL", pnl_sol=pnl,
                                  pnl_pct=(result.to_amount/p.cost_sol-1)*100)
            await self._send_message(chat_id,
                f"✅ 卖出成功！\n{p.symbol}: {pnl:+.4f} SOL"
            )
        else:
            await self._send_message(chat_id, f"❌ 卖出失败：{result.error}")
    
    async def _cmd_help(self, chat_id: int):
        await self._send_message(chat_id,
            "**可用命令：**\n\n"
            "/status - 仓位总览\n"
            "/positions - 持仓详情\n"
            "/close <地址> - 手动平仓\n"
            "/profit - 盈亏统计\n"
            "/pause - 暂停\n"
            "/resume - 恢复\n"
            "/mode - 切换模式\n"
            "/help - 显示此帮助"
        )
