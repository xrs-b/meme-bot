#!/usr/bin/env python3
"""
Telegram Notifier - 专门处理 meme-bot 项目中的所有 Telegram 消息推送
不依赖 OpenClaw，直接使用 Telegram Bot API
"""

import aiohttp
import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


def _fmt_price(v: float) -> str:
    """格式化价格，不用科学计数法"""
    if v <= 0:
        return "$0"
    if v >= 1:
        return f"${v:.2f}"
    else:
        # 小数时保留实际有效数字
        s = f"{v:.10f}".rstrip('0').rstrip('.')
        return f"${s}"


def _fmt_usd(v: float) -> str:
    """格式化美元金额：<10万用K，>=10万用M"""
    if v <= 0:
        return "$-"
    if v >= 100_000:
        return f"${v/1_000_000:.1f}M"
    elif v >= 1_000:
        return f"${v/1_000:.0f}K"
    else:
        return f"${v:.0f}"


def _beijing_time():
    """返回北京时间 (UTC+8) 的当前时间"""
    return datetime.now(timezone(timedelta(hours=8)))
    """格式化美元金额：<10万用K，>=10万用M"""
    if v <= 0:
        return "$-"
    if v >= 100_000:
        return f"${v/1_000_000:.1f}M"
    elif v >= 1_000:
        return f"${v/1_000:.0f}K"
    else:
        return f"${v:.0f}"


class TelegramNotifier:
    """meme-bot 专用 Telegram 通知类"""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}"
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        """发送消息到 Telegram"""
        if not self.bot_token or not self.chat_id:
            logger.warning(f"[TelegramNotifier] 未配置 bot_token 或 chat_id，跳过: {text[:50]}...")
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
                    logger.info(f"[TelegramNotifier] 消息发送成功")
                    return True
                else:
                    logger.error(f"[TelegramNotifier] Telegram 错误: {result.get('description')}")
                    return False
        except Exception as e:
            logger.error(f"[TelegramNotifier] 发送失败: {e}")
            return False

    async def send_signal_alert(self, signal_data: Dict[str, Any]):
        """发送信号提醒"""
        chain = signal_data.get("chain", "UNKNOWN")
        chain_emoji = "🟣" if chain.upper() == "SOLANA" else "🟠"
        signal_type = signal_data.get("type", "UNKNOWN")
        symbol = signal_data.get("symbol", "???")
        name = signal_data.get("name", "???")
        address = signal_data.get("address", "")
        score = signal_data.get("score", 0)
        price = signal_data.get("price", 0)
        liquidity = signal_data.get("liquidity", 0)  # USD
        liquidity_token = signal_data.get("liquidity_token", 0)  # 池子代币数量（SOL或BNB）
        volume_24h = signal_data.get("volume_24h", 0)
        market_cap = signal_data.get("market_cap", 0)

        score_badge = "🟢" if score >= 80 else "🟡" if score >= 60 else "🔴"

        # 池子显示：SOL链用SOL，BSC链用BNB
        if chain.upper() == "SOLANA" and liquidity_token > 0:
            liq_text = f"~{liquidity_token:.1f} SOL ({_fmt_usd(liquidity)})"
        elif chain.upper() == "BSC" and liquidity_token > 0:
            liq_text = f"~{liquidity_token:.2f} BNB ({_fmt_usd(liquidity)})"
        else:
            liq_text = _fmt_usd(liquidity)

        # 市值
        mcap_text = _fmt_usd(market_cap) if market_cap > 0 else "未知"

        # 自动生成中文建议
        if score >= 80:
            if liquidity >= 50000 and volume_24h >= 10000:
                recommendation = "✅ 强烈推荐关注，可考虑轻仓埋伏"
            elif liquidity >= 10000:
                recommendation = "✅ 机会良好，可小仓试探"
            else:
                recommendation = "⚠️ 流动性偏低，控制仓位谨慎参与"
        elif score >= 60:
            if liquidity >= 5000:
                recommendation = "🟡 机会一般，轻仓观察为主"
            else:
                recommendation = "⚠️ 流动性不足，观望"
        elif score >= 40:
            recommendation = "🟠 评分偏低，非热门不参与"
        else:
            recommendation = "🔴 评分过低，忽略"

        # 确保中文和 * 之间有空格，避免 Telegram 解析错误
        text = f"""{signal_type} *信号提醒* {chain_emoji}

━━━━━━ *基本信息* ━━━━━━
🏷️ 代币: *{name}* ({symbol})
🔗 链: {chain} {chain_emoji}
📛 合约: `{address}`
💰 价格: {_fmt_price(price)}
💎 市值: {mcap_text}
💧 流动性: {liq_text}
📊 24h 交易量: {_fmt_usd(volume_24h)}

━━━━━━ *信号评分* ━━━━━━
{score_badge} 评分: *{score}/100*
📊 置信度: {score}%

━━━━━━ *交易建议* ━━━━━━
💎 建议: {recommendation}

🕐 {_beijing_time().strftime('%Y-%m-%d %H:%M:%S')}
"""
        await self.send_message(text.strip())

    async def send_trade_alert(self, trade_data: Dict[str, Any]):
        """发送交易提醒"""
        chain = trade_data.get("chain", "UNKNOWN")
        chain_emoji = "🟣" if chain.upper() == "SOLANA" else "🟠"
        action = trade_data.get("action", "buy").upper()
        action_emoji = "🟢" if action == "BUY" else "🔴"
        symbol = trade_data.get("symbol", "???")
        amount = trade_data.get("amount_in", 0)
        value = trade_data.get("value_usd", 0)
        price = trade_data.get("price", 0)
        tx_hash = trade_data.get("tx_hash", "")

        text = f"""{action_emoji} *{action} 交易执行* {chain_emoji}

🏷️ 代币: *{symbol}*
💵 数量: {amount}
💰 价格: {_fmt_price(price)}
💵 价值: ${value:,.2f}

🔗 Tx: `{tx_hash}`
🕐 {_beijing_time().strftime('%Y-%m-%d %H:%M:%S')}
"""
        await self.send_message(text.strip())

    async def send_rug_pull_alert(self, token_data: Dict[str, Any]):
        """发送 Rug Pull 警告"""
        chain = token_data.get("chain", "UNKNOWN")
        chain_emoji = "🟣" if chain.upper() == "SOLANA" else "🟠"
        symbol = token_data.get("symbol", "???")
        name = token_data.get("name", "???")
        address = token_data.get("address", "")
        message = token_data.get("message", "")

        text = f"""🚨 *RUG PULL 警告* {chain_emoji}

🏷️ 代币: *{name}* ({symbol})
📛 合约: `{address}`

⚠️ {message}

💸 流动性池已被大幅抽离！建议立即检查持仓。
"""
        await self.send_message(text.strip())

    async def send_status(self, status_text: str):
        """发送状态更新"""
        text = f"🤖 *BOT 状态*\n\n{status_text}"
        await self.send_message(text)

    async def send_test(self) -> bool:
        """发送测试消息，验证配置是否正确"""
        text = "✅ *Telegram Notifier 连接成功!*\n\nmeme-bot 消息推送已激活。"
        return await self.send_message(text)


def load_notifier_from_config(config_path: str = "config.json") -> Optional[TelegramNotifier]:
    """从配置文件加载 Telegram Notifier"""
    path = Path(config_path)
    if not path.exists():
        logger.error(f"[TelegramNotifier] 配置文件不存在: {config_path}")
        return None

    with open(path) as f:
        config = json.load(f)

    bot_token = config.get("telegram_bot_token", "")
    chat_id = config.get("telegram_chat_id", "")

    if not bot_token or not chat_id:
        logger.error(f"[TelegramNotifier] 配置文件中缺少 telegram_bot_token 或 telegram_chat_id")
        return None

    return TelegramNotifier(bot_token, chat_id)


# 测试代码
if __name__ == "__main__":
    async def test():
        notifier = load_notifier_from_config()
        if notifier:
            result = await notifier.send_test()
            print(f"测试结果: {'成功' if result else '失败'}")
        else:
            print("无法加载 notifier，请检查 config.json")

    asyncio.run(test())
