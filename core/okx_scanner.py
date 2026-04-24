#!/usr/bin/env python3
"""
OKX OnchainOS Integration for Meme Bot
Combines multiple OKX skills for comprehensive meme coin discovery and analysis.
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

# OKX credentials path
ENV_FILE = Path(__file__).parent.parent / ".env"


def _load_env() -> Dict[str, str]:
    """Load OKX credentials from .env file"""
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
    """Run onchainos CLI command and return parsed JSON"""
    try:
        result = subprocess.run(
            ["onchainos"] + args,
            capture_output=True,
            text=True,
            env={**os.environ, **env},
            timeout=30
        )
        if result.returncode != 0:
            logger.warning(f"onchainos command failed: {result.stderr}")
            return {}
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning(f"Failed to parse onchainos output: {result.stdout[:200]}")
        return {}
    except subprocess.TimeoutExpired:
        logger.warning(f"onchainos command timed out: {' '.join(args)}")
        return {}


def _fmt_usd(v: float) -> str:
    """Format USD value: <100K use K, >=100K use M"""
    if v <= 0:
        return "$-"
    if v >= 100_000:
        return f"${v/1_000_000:.1f}M"
    elif v >= 1_000:
        return f"${v/1_000:.0f}K"
    else:
        return f"${v:.0f}"


def _fmt_price(v: float) -> str:
    """Format price without scientific notation"""
    if v <= 0:
        return "$0"
    if v >= 1:
        return f"${v:.2f}"
    else:
        s = f"{v:.10f}".rstrip('0').rstrip('.')
        return f"${s}"


@dataclass
class MemeToken:
    """Represents a meme token with analysis data"""
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
    """
    OKX OnchainOS Scanner for meme tokens.
    Coordinates: okx-dex-trenches (discovery) -> okx-dex-token (data) -> okx-security (risk)
    """

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

        self._env_set = bool(self.env)

    @property
    def is_configured(self) -> bool:
        return bool(self.env.get("OKX_API_KEY"))

    async def scan_pumpfun_new_tokens(self, limit: int = 30) -> List[MemeToken]:
        """
        Scan pump.fun for new tokens (meme coin discovery).
        Uses: okx-dex-trenches (onchainos memepump tokens)
        """
        if not self.is_configured:
            logger.error("OKX not configured - cannot scan pump.fun")
            return []

        result = _run_onchainos(
            ["memepump", "tokens", "--chain", "solana", "--stage", "NEW"],
            self.env
        )

        tokens_data = result.get("data", [])
        if not tokens_data:
            return []

        tokens = []
        for t in tokens_data[:limit]:
            try:
                addr = t.get("tokenAddress", "")
                if not addr:
                    continue

                # Get price info
                price_data = _run_onchainos(
                    ["token", "price-info", "--chain", "solana", "--address", addr],
                    self.env
                )
                pdata = price_data.get("data", [{}])[0] if price_data.get("data") else {}

                mc = float(pdata.get("marketCap", 0))
                liq = float(pdata.get("liquidity", 0))
                vol = float(pdata.get("volume24H", 0))
                holders = int(pdata.get("holders", 0))

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
                    chain="SOLANA",
                    market_cap=mc,
                    liquidity=liq,
                    volume_24h=vol,
                    holders=holders,
                    bonding_percent=float(t.get("bondingPercent", 0) or 0),
                    price=float(pdata.get("price", 0) or 0),
                    dev_holdings=dev_hold,
                    top10_holdings=top10,
                    snipers_percent=snipers,
                    bundlers_percent=bundlers,
                    fresh_wallets_percent=fresh,
                )
                token.score = self._calculate_score(token)
                tokens.append(token)
            except Exception as e:
                logger.warning(f"Failed to parse token: {e}")
                continue

        # Sort by score descending
        tokens.sort(key=lambda x: x.score, reverse=True)
        return tokens

    async def security_scan(self, address: str, chain: str = "solana") -> Tuple[str, List[str]]:
        """
        Security scan a token for risks.
        Uses: okx-security (onchainos security token-scan)
        Returns: (risk_level, reasons)
        """
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

    async def get_token_advanced_info(self, address: str, chain: str = "solana") -> Optional[Dict[str, Any]]:
        """
        Get advanced token info (developer info, holder distribution).
        Uses: okx-dex-token (onchainos token advanced-info)
        """
        if not self.is_configured:
            return None

        result = _run_onchainos(
            ["token", "advanced-info", "--chain", chain, "--address", address],
            self.env
        )

        return result.get("data", [{}])[0] if result.get("data") else None

    async def get_dev_info(self, address: str, chain: str = "solana") -> Optional[Dict[str, Any]]:
        """
        Get developer reputation info.
        Uses: okx-dex-trenches (onchainos memepump token-dev-info)
        """
        if not self.is_configured:
            return None

        result = _run_onchainos(
            ["memepump", "token-dev-info", "--address", address],
            self.env
        )

        return result.get("data", [{}])[0] if result.get("data") else None

    async def scan_and_analyze(self, top_n: int = 10, min_score: int = 50) -> List[MemeToken]:
        """
        Full pipeline: discover -> enrich -> security scan -> rank.
        Returns top N tokens with full analysis.
        """
        if not self.is_configured:
            logger.error("OKX not configured")
            return []

        logger.info("Scanning pump.fun for new tokens...")
        tokens = await self.scan_pumpfun_new_tokens(limit=30)

        if not tokens:
            logger.warning("No tokens found from pump.fun")
            return []

        # Filter by initial score
        promising = [t for t in tokens if t.score >= min_score]
        logger.info(f"Found {len(promising)} promising tokens (score >= {min_score})")

        # Enrich with security scan
        analyzed = []
        for t in promising[:top_n]:
            risk_level, reasons = await self.security_scan(t.address, "solana")
            t.risk_level = risk_level
            t.risk_reasons = reasons

            if risk_level == "high":
                t.score = max(0, t.score - 30)  # Heavy penalty
            elif risk_level == "medium":
                t.score = max(0, t.score - 15)  # Medium penalty

            # Dev info
            dev_info = await self.get_dev_info(t.address, "solana")
            if dev_info:
                t.dev_reputation = dev_info.get("devReputation", "")

            analyzed.append(t)

        # Re-sort after security adjustments
        analyzed.sort(key=lambda x: x.score, reverse=True)
        return analyzed[:top_n]

    def _calculate_score(self, token: MemeToken) -> int:
        """Calculate meme potential score (0-100)"""
        score = 0

        # Liquidity (max 25)
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

        # Volume (max 25)
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

        # Holders (max 20)
        if token.holders >= 50:
            score += 20
        elif token.holders >= 20:
            score += 15
        elif token.holders >= 10:
            score += 10
        elif token.holders >= 5:
            score += 5

        # Market cap (max 15)
        if token.market_cap >= 10000:
            score += 15
        elif token.market_cap >= 5000:
            score += 10
        elif token.market_cap >= 1000:
            score += 5

        # Holder distribution penalties (max -15)
        if token.top10_holdings > 50:
            score -= 10
        elif token.top10_holdings > 30:
            score -= 5

        if token.dev_holdings > 20:
            score -= 5

        if token.snipers_percent > 10:
            score -= 3

        # Bonding progress bonus (15)
        bp = token.bonding_percent
        if bp >= 80:
            score += 15
        elif bp >= 50:
            score += 10
        elif bp >= 25:
            score += 5

        return max(0, min(100, score))

    def format_signal(self, token: MemeToken) -> str:
        """Format token as Telegram signal message"""
        score_badge = "🟢" if token.score >= 70 else "🟡" if token.score >= 50 else "🔴"
        risk_badge = "✅" if token.risk_level == "safe" else "⚠️" if token.risk_level == "medium" else "🚨"

        mc_s = _fmt_usd(token.market_cap)
        liq_s = _fmt_usd(token.liquidity)
        vol_s = _fmt_usd(token.volume_24h)

        recommendation = self._get_recommendation(token)

        lines = [
            f"🆕 *NEW COIN* 信号提醒 🟣",
            "",
            "━━━━━━ *基本信息* ━━━━━━",
            f"🏷️ 代币: *{token.name}* ({token.symbol})",
            f"🔗 链: {token.chain} 🟣",
            f"📛 合约: `{token.address}`",
            f"💰 价格: {_fmt_price(token.price)}",
            f"💎 市值: {mc_s}",
            f"💧 流动性: {liq_s}",
            f"📊 24h 交易量: {vol_s}",
            "",
            "━━━━━━ *持仓分布* ━━━━━━",
            f"👥 holders: {token.holders}",
            f"🔝 Top10: {token.top10_holdings:.1f}%",
            f"👤 开发者: {token.dev_holdings:.1f}%",
            f"🎯 bundler: {token.bundlers_percent:.1f}%",
            f"📈 bonding: {token.bonding_percent:.1f}%",
            "",
            "━━━━━━ *信号评分* ━━━━━━",
            f"{score_badge} 评分: *{token.score}/100*",
            f"{risk_badge} 风险: {token.risk_level.upper()}",
            "",
            "━━━━━━ *交易建议* ━━━━━━",
            f"💎 建议: {recommendation}",
            "",
            f"🕐 {_beijing_time().strftime('%Y-%m-%d %H:%M:%S')}",
        ]

        if token.risk_reasons:
            lines.insert(-2, f"⚠️ 风险: {', '.join(token.risk_reasons[:2])}")

        return "\n".join(lines)

    def _get_recommendation(self, token: MemeToken) -> str:
        """Generate trading recommendation based on score and risk"""
        if token.risk_level == "high":
            return "🚨 风险过高，建议回避"
        if token.score >= 75:
            if token.liquidity >= 5000 and token.holders >= 10:
                return "✅ 强烈推荐关注，可考虑轻仓埋伏"
            else:
                return "✅ 机会良好，可小仓试探"
        elif token.score >= 60:
            return "🟡 机会一般，轻仓观察为主"
        elif token.score >= 40:
            return "🟠 评分偏低，非热门不参与"
        else:
            return "🔴 评分过低，忽略"

    def format_top_scan(self, tokens: List[MemeToken]) -> str:
        """Format multiple tokens as a ranked scan report"""
        if not tokens:
            return "未发现符合条件的代币"

        lines = [
            f"🆕 *pump.fun 新盘扫描* (TOP {len(tokens)})",
            "",
        ]

        for i, t in enumerate(tokens, 1):
            badge = "🟢" if t.score >= 70 else "🟡" if t.score >= 50 else "🔴"
            mc_s = _fmt_usd(t.market_cap)
            liq_s = _fmt_usd(t.liquidity)
            vol_s = _fmt_usd(t.volume_24h)

            lines.append(f"{badge} {i}. *{t.symbol}* ({t.name[:15]})")
            lines.append(f"   市值{mc_s} | 流动性{liq_s} | 24h{vol_s}")
            lines.append(f"   holders:{t.holders} | bonding:{t.bonding_percent:.1f}% | 评分:{t.score}")
            lines.append("")

        lines.append(f"🕐 {_beijing_time().strftime('%Y-%m-%d %H:%M:%S')}")
        return "\n".join(lines)


def _beijing_time():
    return datetime.now(timezone(timedelta(hours=8)))
