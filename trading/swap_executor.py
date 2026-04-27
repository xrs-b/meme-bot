"""
Swap 执行器
通过 onchainos CLI 执行 pump.fun 买/卖操作
"""
import subprocess
import json
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional
from enum import Enum

logger = logging.getLogger(__name__)

# Solana 常用代币地址
WRAPPED_SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"


class SwapDirection(Enum):
    BUY = "buy"    # SOL/USDC → MEME token
    SELL = "sell"  # MEME token → SOL/USDC


@dataclass
class SwapQuote:
    """Swap 报价"""
    from_token: str
    to_token: str
    from_amount: float        # 输入金额（SOL 或 USDC）
    to_amount: float          # 输出金额
    price_impact_pct: float   # 价格影响 %
    dex_router: str           # 使用的 DEX
    estimated_sol: float     # 预估消耗 SOL（包含手续费）


@dataclass
class SwapResult:
    """Swap 执行结果"""
    success: bool
    tx_hash: Optional[str] = None
    from_token: str = ""
    to_token: str = ""
    from_amount: float = 0
    to_amount: float = 0
    price_impact_pct: float = 0
    error: Optional[str] = None
    gas_used_sol: float = 0


def _run_onchainos(args: list, timeout: int = 30) -> dict:
    """执行 onchainos CLI 命令"""
    import os
    env = os.environ.copy()
    env["PATH"] = "/root/.local/bin:" + env.get("PATH", "")
    cmd = " ".join(args)
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, env=env, timeout=timeout
    )
    if result.returncode != 0:
        logger.error(f"onchainos error: {result.stderr[:200]}")
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.error(f"JSON decode error: {result.stdout[:200]}")
        return {}


class SwapExecutor:
    """Swap 执行器"""
    
    def __init__(self, wallet_address: str):
        self.wallet_address = wallet_address
        self.chain = "solana"
    
    async def get_quote(self, from_token: str, to_token: str,
                        amount: float) -> Optional[SwapQuote]:
        """
        获取 swap 报价
        from_token/to_token: 代币合约地址
        amount: 输入金额（SOL 或 USDC）
        """
        # USDC 精度 6, SOL 精度 9
        decimals = 9 if from_token == WRAPPED_SOL else 6
        
        result = _run_onchainos([
            "onchainos", "swap", "quote",
            "--chain", self.chain,
            "--from", from_token,
            "--to", to_token,
            "--readable-amount", str(amount)
        ])
        
        if not result.get("ok"):
            return None
        
        data = result.get("data", [])
        if not data:
            return None
        
        quote = data[0]
        dex_list = quote.get("dexRouterList", [])
        best_dex = dex_list[0] if dex_list else {}
        dex_protocol = best_dex.get("dexProtocol", {})
        
        from_info = best_dex.get("fromToken", {})
        to_info = best_dex.get("toToken", {})
        
        return SwapQuote(
            from_token=from_token,
            to_token=to_token,
            from_amount=amount,
            to_amount=float(from_info.get("tokenUnitPrice", 0) or 0) * amount / float(to_info.get("tokenUnitPrice", 1) or 1) * amount,
            price_impact_pct=0,  # quote 里没有这个字段
            dex_router=dex_protocol.get("dexName", "unknown"),
            estimated_sol=amount
        )
    
    async def execute_swap(self, from_token: str, to_token: str,
                          amount: float, slippage_pct: float = 2.0,
                          direction: SwapDirection = SwapDirection.BUY) -> SwapResult:
        """
        执行 swap
        - direction=BUY: 用 SOL/USDC 买 MEME token
        - direction=SELL: 卖出 MEME token 换 SOL/USDC
        
        slippage_pct: 滑点容忍 %
        """
        # 检查钱包余额
        balance = await self.get_balance(from_token if direction == SwapDirection.SELL else WRAPPED_SOL)
        if balance < amount:
            return SwapResult(
                success=False,
                error=f"余额不足：需要 {amount}，实际 {balance}",
                from_token=from_token, to_token=to_token, from_amount=amount
            )
        
        result = _run_onchainos([
            "onchainos", "swap", "execute",
            "--chain", self.chain,
            "--wallet", self.wallet_address,
            "--from", from_token,
            "--to", to_token,
            "--readable-amount", str(amount),
            "--slippage", str(slippage_pct),
            "--mev-protection" if direction == SwapDirection.BUY else "",
        ], timeout=60)
        
        if not result.get("ok"):
            error_msg = result.get("message", str(result))
            return SwapResult(
                success=False,
                error=f"swap execute failed: {error_msg[:200]}",
                from_token=from_token, to_token=to_token, from_amount=amount
            )
        
        # 解析结果
        data = result.get("data", {})
        tx_hash = data.get("txHash") or data.get("hash") or data.get("tx_hash")
        
        return SwapResult(
            success=True,
            tx_hash=tx_hash,
            from_token=from_token,
            to_token=to_token,
            from_amount=amount,
            to_amount=data.get("toAmount", data.get("outputAmount", 0)),
            price_impact_pct=data.get("priceImpact", 0),
            gas_used_sol=data.get("gasUsed", 0)
        )
    
    async def get_sol_balance(self) -> float:
        """查询钱包 SOL 余额"""
        import subprocess, json, os
        env = os.environ.copy()
        env["PATH"] = "/root/.local/bin:" + env.get("PATH", "")
        result = subprocess.run([
            "curl", "-s", "-X", "POST",
            "https://api.mainnet-beta.solana.com",
            "-H", "Content-Type: application/json",
            "-d", json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "getBalance",
                "params": [self.wallet_address]
            })
        ], capture_output=True, text=True, timeout=10, env=env)
        try:
            data = json.loads(result.stdout)
            lamports = data.get("result", {}).get("value", 0)
            return lamports / 1e9
        except:
            return 0.0

    async def get_balance(self, token_address: str) -> float:
        """
        查询钱包余额
        token_address: 代币地址，SOL用 WRAPPED_SOL
        返回：余额（SOL 或代币数量）
        """
        if token_address == WRAPPED_SOL or token_address == USDC or token_address == USDT:
            # 通过 RPC 查询
            import subprocess
            if token_address == WRAPPED_SOL:
                result = subprocess.run([
                    "curl", "-s", "-X", "POST",
                    "https://api.mainnet-beta.solana.com",
                    "-H", "Content-Type: application/json",
                    "-d", json.dumps({
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getBalance",
                        "params": [self.wallet_address]
                    })
                ], capture_output=True, text=True, timeout=10)
                data = json.loads(result.stdout)
                lamports = data.get("result", {}).get("value", 0)
                return lamports / 1e9
            else:
                # SPL 代币查询需要解析 account data，略复杂
                # 暂时返回 0，让 execute_swap 失败
                return 0
        return 0
    
    async def buy_token(self, token_address: str, sol_amount: float,
                       slippage_pct: float = 5.0) -> SwapResult:
        """
        买入 MEME token（用 SOL 买）
        """
        return await self.execute_swap(
            from_token=WRAPPED_SOL,
            to_token=token_address,
            amount=sol_amount,
            slippage_pct=slippage_pct,
            direction=SwapDirection.BUY
        )
    
    async def sell_token(self, token_address: str, token_amount: float,
                        min_sol_amount: float = 0.001,
                        slippage_pct: float = 5.0) -> SwapResult:
        """
        卖出 MEME token（换 SOL）
        """
        return await self.execute_swap(
            from_token=token_address,
            to_token=WRAPPED_SOL,
            amount=token_amount,
            slippage_pct=slippage_pct,
            direction=SwapDirection.SELL
        )
