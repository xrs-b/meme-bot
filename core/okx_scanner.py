#!/usr/bin/env python3
"""
OKX OnchainOS Integration for Meme Bot
"""

import asyncio
import subprocess
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_FILE = Path(__file__).parent.parent / ".env"


def _load_env() -> Dict[str, str]:
    env = {}
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _run_onchainos(args: List[str], env: Dict[str, str]) -> Dict[str, Any]:
    try:
        result = subprocess.run(
            ["onchainos"] + args,
            capture_output=True,
            text=True,
            env={**os.environ, **env},
            timeout=30
        )
        if result.returncode != 0:
            return {}
        return json.loads(result.stdout)
    except:
        return {}


def _fmt_usd(v: float) -> str:
    if v <= 0:
        return "$-"
    if v >= 100_000:
        return f"${v/1_000_000:.1f}M"
    elif v >= 1_000:
        return f"${v/1_000:.0f}K"
    else:
        return f"${v:.0f}"


def _fmt_price(v: float) -> str:
    if v <= 0:
        return "$0"
    if v >= 1:
        return f"${v:.2f}"
    else:
        s = f"{v:.10f}".rstrip('0').rstrip('.')
        if len(s) > 8:
            zeros = 0
            for c in s[2:]:
                if c == '0':
                    zeros += 1
                else:
                    break
            if zeros >= 4:
                return f"$0.0{{{zeros}}}{s[2+zeros:]}"
        return s


def _fmt_age(create_time_ms: int) -> str:
    if not create_time_ms:
        return "未知"
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    age_ms = now_ms - create_time_ms
    if age_ms < 0:
        return "刚刚"
    total_seconds = age_ms / 1000
    days = int(total_seconds // 86400)
    hours = int((total_seconds % 86400) // 3600)
    minutes = int((total_seconds % 3600) // 60)
    if days > 0:
        return f"{days}天{hours}小时{minutes}分钟"
    elif hours > 0:
        return f"{hours}小时{minutes}分钟"
    else:
        return f"{minutes}分钟"


def _beijing_time():
    return datetime.now(timezone(timedelta(hours=8)))


@dataclass
class MemeToken:
    symbol: str
    name: str
    address: str
    chain: str
    market_cap: float
    liquidity: float
    volume_24h: float
    holders: int
    bonding_percent: float
    price: float
    create_time: int = 0
    mint_auth: bool = False
    freeze_auth: bool = False
    lp_burned: bool = False
    top_holder_percent: float = 0
    pool_liquidity: float = 0
    pool_liquidity_token: float = 0
    pool_chain: str = ""
    twitter: str = ""
    website: str = ""
    dev_holdings: float = 0
    top10_holdings: float = 0
    snipers_percent: float = 0
    bundlers_percent: float = 0
    fresh_wallets_percent: float = 0
    risk_level: str = "unknown"
    risk_reasons: List[str] = None
    score: int = 0

    def __post_init__(self):
        if self.risk_reasons is None:
            self.risk_reasons = []


class OKXScanner:
    SUPPORTED_CHAINS = ["solana", "bnb"]
    CHAIN_EMOJI = {"solana": "🟣", "bnb": "🟠"}
    CHAIN_INDEX_MAP = {"501": "solana", "56": "bnb"}

    def __init__(self, api_key: str = "", secret_key: str = "", passphrase: str = ""):
        self.env = {}
        if api_key and secret_key and passphrase:
            self.env = {
                "OKX_API_KEY": api_key,
                "OKX_SECRET_KEY": secret_key,
                "OKX_PASSPHRASE": passphrase
            }
        else:
            self.env = _load_env()

    @property
    def is_configured(self) -> bool:
        return bool(self.env.get("OKX_API_KEY"))

    def _chain_from_index(self, chain_index: str) -> str:
        return self.CHAIN_INDEX_MAP.get(chain_index, "unknown")

    async def scan_pumpfun_new_tokens(self, chains: List[str] = None, limit: int = 30) -> List[MemeToken]:
        if not self.is_configured:
            return []

        if chains is None:
            chains = self.SUPPORTED_CHAINS

        all_tokens = []
        
        # Get SOL price for USD conversion
        sol_price = 0
        if "solana" in chains:
            sol_data = _run_onchainos(
                ["token", "price-info", "--chain", "solana", "--address", "So11111111111111111111111111111111111111112"],
                self.env
            )
            sol_price = float(sol_data.get("data", [{}])[0].get("price", 0) or 0)
        
        for chain in chains:
            result = _run_onchainos(
                ["memepump", "tokens", "--chain", chain, "--stage", "NEW"],
                self.env
            )

            tokens_data = result.get("data", [])
            if not tokens_data:
                continue

            for t in tokens_data[:limit]:
                try:
                    addr = t.get("tokenAddress", "")
                    if not addr:
                        continue

                    chain_index = t.get("chainIndex", "")
                    actual_chain = self._chain_from_index(chain_index) if chain_index else chain
                    if actual_chain == "unknown":
                        continue

                    price_data = _run_onchainos(
                        ["token", "price-info", "--chain", actual_chain, "--address", addr],
                        self.env
                    )
                    pdata = price_data.get("data", [{}])[0] if price_data.get("data") else {}

                    create_time = 0
                    adv_data = _run_onchainos(
                        ["token", "advanced-info", "--chain", actual_chain, "--address", addr],
                        self.env
                    )
                    adv = adv_data.get("data", {})
                    if adv:
                        create_time = int(adv.get("createTime", 0) or 0)
                        
                        # Security fields from advanced-info
                        lp_burned_str = adv.get("lpBurnedPercent", "")
                        token.lp_burned = lp_burned_str and float(lp_burned_str) > 0
                        
                        top10_str = adv.get("top10HoldPercent", "")
                        token.top_holder_percent = float(top10_str) if top10_str else 0
                        
                        # riskControlLevel: 1=low, 2=medium, 3+=high
                        risk_level_str = adv.get("riskControlLevel", "1")
                        try:
                            rlv = int(risk_level_str)
                            if rlv == 1:
                                token.risk_level = "safe"
                            elif rlv == 2:
                                token.risk_level = "medium"
                            else:
                                token.risk_level = "high"
                        except:
                            token.risk_level = "unknown"
                        
                        # Check tags for honeypot/rug indicators
                        tags = adv.get("tokenTags", [])
                        if "devHoldingStatusSellAll" in tags:
                            token.risk_level = "high"

                    mc = float(pdata.get("marketCap", 0) or 0)
                    # liquidity and volume24H are in SOL, convert to USD
                    liq_sol = float(pdata.get("liquidity", 0) or 0)
                    vol_sol = float(pdata.get("volume24H", 0) or 0)
                    liq = liq_sol * sol_price if sol_price > 0 else 0
                    vol = vol_sol * sol_price if sol_price > 0 else 0
                    holders = int(pdata.get("holders", 0) or 0)

                    tags = t.get("tags", {})
                    dev_hold = float(tags.get("devHoldingsPercent") or 0)
                    top10 = float(tags.get("top10HoldingsPercent") or 0)
                    snipers = float(tags.get("snipersPercent") or 0)
                    bundlers = float(tags.get("bundlersPercent") or 0)
                    fresh = float(tags.get("freshWalletsPercent") or 0)

                    token = MemeToken(
                        symbol=t.get("symbol", "???"),
                        name=t.get("name", "???"),
                        address=addr,
                        chain=actual_chain.upper(),
                        market_cap=mc,
                        liquidity=liq,
                        volume_24h=vol,
                        holders=holders,
                        bonding_percent=float(t.get("bondingPercent", 0) or 0),
                        price=float(pdata.get("price", 0) or 0),
                        create_time=create_time,
                        dev_holdings=dev_hold,
                        top10_holdings=top10,
                        snipers_percent=snipers,
                        bundlers_percent=bundlers,
                        fresh_wallets_percent=fresh,
                    )
                    token.score = self._calculate_score(token)
                    all_tokens.append(token)
                except:
                    continue

        all_tokens.sort(key=lambda x: x.score, reverse=True)
        return all_tokens[:limit]

    async def get_smart_money_signals(self, chain: str = "solana", limit: int = 10) -> List[Dict[str, Any]]:
        if not self.is_configured:
            return []

        result = _run_onchainos(
            ["signal", "list", "--chain", chain, "--limit", str(limit)],
            self.env
        )

        signals = result.get("data", [])
        parsed = []
        for s in signals:
            token = s.get("token", {})
            parsed.append({
                "symbol": token.get("symbol", "???"),
                "name": token.get("name", "???"),
                "address": token.get("tokenAddress", ""),
                "chain": chain,
                "price": float(token.get("price", 0) or 0),
                "market_cap": float(token.get("marketCapUsd", 0) or 0),
                "holders": int(token.get("holders", 0) or 0),
                "top10_holders": float(token.get("top10HolderPercent", 0) or 0),
                "amount_usd": float(s.get("amountUsd", 0) or 0),
                "sold_ratio": float(s.get("soldRatioPercent", 0) or 0),
                "wallet_count": int(s.get("triggerWalletCount", 0) or 0),
                "timestamp": int(s.get("timestamp", 0) or 0),
            })
        return parsed

    def format_smart_money_signals(self, signals: List[Dict[str, Any]]) -> str:
        if not signals:
            return "暂无聪明钱信号"
        
        lines = [
            "🐋 聪明钱/鲸鱼信号追踪",
            ""
        ]
        
        filtered = [s for s in signals if s.get("sold_ratio", 0) <= 80]
        
        for i, s in enumerate(filtered[:5], 1):
            symbol = s.get("symbol", "???").upper()
            name = s.get("name", "???")[:12]
            addr = s.get("address", "")
            chain = s.get("chain", "SOL").upper()
            mc = s.get("market_cap", 0)
            holders = s.get("holders", 0)
            top10 = s.get("top10_holders", 0)
            amount = s.get("amount_usd", 0)
            wallet_cnt = s.get("wallet_count", 0)
            
            mc_s = _fmt_usd(mc)
            amount_s = f"${amount:.0f}" if amount < 1000 else f"${amount/1000:.1f}K"
            
            lines.append(f"━━━━━━ 信号 {i} ━━━━━━")
            lines.append(f"🏷️ 代币: {symbol} ({name})")
            lines.append(f"🔗 链: {chain}")
            lines.append(f"📛 合约: `{addr}`")
            lines.append(f"💎 市值: {mc_s}")
            lines.append(f"👥 holders: {holders} | Top10: {top10:.1f}%")
            lines.append(f"💰 聪明钱买入: {amount_s} x {wallet_cnt}人")
            lines.append("")
        
        lines.append(f"🕐 {_beijing_time().strftime('%Y-%m-%d %H:%M:%S')}")
        return "\n".join(lines)

    async def security_scan(self, address: str, chain: str = "solana") -> Tuple[str, List[str]]:
        if not self.is_configured:
            return "unknown", ["OKX未配置"]

        result = _run_onchainos(
            ["security", "token-scan", "--chain", chain, "--address", address],
            self.env
        )

        risks = result.get("data", [])
        if not risks:
            return "safe", []

        reasons = []
        risk_level = "safe"
        for r in risks:
            level = r.get("riskLevel", "")
            desc = r.get("riskDescription", "")
            if level in ["high", "medium"]:
                risk_level = level
                reasons.append(desc)

        return risk_level, reasons

    def _calculate_score(self, token: MemeToken) -> int:
        score = 0

        if token.liquidity >= 10000:
            score += 25
        elif token.liquidity >= 5000:
            score += 20
        elif token.liquidity >= 1000:
            score += 15
        elif token.liquidity >= 100:
            score += 10
        elif token.liquidity > 0:
            score += 5

        if token.volume_24h >= 10000:
            score += 25
        elif token.volume_24h >= 5000:
            score += 20
        elif token.volume_24h >= 1000:
            score += 15
        elif token.volume_24h >= 100:
            score += 10
        elif token.volume_24h > 0:
            score += 5

        if token.holders >= 50:
            score += 20
        elif token.holders >= 20:
            score += 15
        elif token.holders >= 10:
            score += 10
        elif token.holders >= 5:
            score += 5

        if token.market_cap >= 10000:
            score += 15
        elif token.market_cap >= 5000:
            score += 10
        elif token.market_cap >= 1000:
            score += 5

        if token.top10_holdings > 50:
            score -= 10
        elif token.top10_holdings > 30:
            score -= 5

        if token.dev_holdings > 20:
            score -= 5

        if token.snipers_percent > 10:
            score -= 3

        bp = token.bonding_percent
        if bp >= 80:
            score += 15
        elif bp >= 50:
            score += 10
        elif bp >= 25:
            score += 5

        return max(0, min(100, score))

    def format_signal(self, token: MemeToken) -> str:
        chain_name = token.chain.upper()
        score_badge = "🟢" if token.score >= 70 else "🟡" if token.score >= 50 else "🔴"
        risk_badge = "✅" if token.risk_level == "safe" else "⚠️" if token.risk_level == "medium" else "🚨"

        mc_s = _fmt_usd(token.market_cap)
        liq_s = _fmt_usd(token.liquidity)
        vol_s = _fmt_usd(token.volume_24h)

        recommendation = self._get_recommendation(token)

        twitter = token.twitter if token.twitter else "无"
        website = token.website if token.website else "无"

        # Pool in SOL/BTC
        pool_text = liq_s
        if token.pool_liquidity_token > 0 and token.pool_chain:
            if token.pool_chain.lower() == "solana":
                pool_text = f"{liq_s} ({token.pool_liquidity_token:.1f} SOL)"
            elif token.pool_chain.lower() == "bnb":
                pool_text = f"{liq_s} ({token.pool_liquidity_token:.2f} BNB)"

        lines = [
            f"🆕 NEW COIN | {chain_name}",
            "",
            "━━━━━━ 基本信息 ━━━━━━",
            f"🏷️ 代币: {token.name} ({token.symbol})",
            f"🔗 链: {chain_name}",
            f"📛 合约: `{token.address}`",
            f"⏱️ 发行: {_fmt_age(token.create_time)}",
            f"💰 价格: {_fmt_price(token.price)}",
            f"💎 市值: {mc_s}",
            f"💧 流动性: {pool_text}",
            f"📊 24h 交易量: {vol_s}",
            "",
            "━━━━━━ 安全检测 ━━━━━━",
            f"🔓 Mint弃权: {'✅' if token.mint_auth else '❌'} | "
            f"🚫 黑名单: {'✅' if token.freeze_auth else '❌'} | "
            f"🔥 烧池子: {'✅' if token.lp_burned else '❌'}",
            f"🐀 老鼠仓: {token.top_holder_percent:.1f}%",
            "",
            "━━━━━━ 链接 ━━━━━━",
            f"🐦 推特: {twitter}",
            f"🌏 官网: {website}",
            "",
            "━━━━━━ 信号评分 ━━━━━━",
            f"{score_badge} 评分: {token.score}/100",
            f"{risk_badge} 风险: {token.risk_level.upper()}",
            "",
            "━━━━━━ 交易建议 ━━━━━━",
            f"💎 建议: {recommendation}",
            "",
            f"🕐 {_beijing_time().strftime('%Y-%m-%d %H:%M:%S')}",
        ]

        return "\n".join(lines)

    def _get_recommendation(self, token: MemeToken) -> str:
        if token.risk_level == "high":
            return "风险过高，建议回避"
        if token.score >= 75:
            if token.liquidity >= 5000 and token.holders >= 10:
                return "强烈推荐关注，可考虑轻仓埋伏"
            else:
                return "机会良好，可小仓试探"
        elif token.score >= 60:
            return "机会一般，轻仓观察为主"
        elif token.score >= 40:
            return "评分偏低，非热门不参与"
        else:
            return "评分过低，忽略"

    def format_top_scan(self, tokens: List[MemeToken]) -> str:
        if not tokens:
            return "未发现符合条件的代币"

        lines = [
            f"🆕 pump.fun 新盘扫描 (TOP {len(tokens)})",
            "",
        ]

        for i, t in enumerate(tokens, 1):
            badge = "🟢" if t.score >= 70 else "🟡" if t.score >= 50 else "🔴"
            mc_s = _fmt_usd(t.market_cap)
            liq_s = _fmt_usd(t.liquidity)
            vol_s = _fmt_usd(t.volume_24h)
            chain_name = t.chain.upper()

            lines.append(f"{badge} {i}. {t.symbol} ({t.name[:15]}) [{chain_name}]")
            lines.append(f"   市值{mc_s} | 流动性{liq_s} | 24h{vol_s}")
            lines.append(f"   holders:{t.holders} | bonding:{t.bonding_percent:.1f}% | 评分:{t.score}")
            lines.append("")

        lines.append(f"🕐 {_beijing_time().strftime('%Y-%m-%d %H:%M:%S')}")
        return "\n".join(lines)
