#!/usr/bin/env python3
"""
BSC (Binance Smart Chain) Adapter for Meme Bot
Handles all BSC blockchain interactions: RPC, PancakeSwap, wallet tracking
"""

import asyncio
import json
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass
import logging

import aiohttp
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ABIFunctionNotFound

# PancakeSwap ABIs
PANCAKE_SWAP_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E192024e"
PANCAKE_SWAP_FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
BUSD = "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56"
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"

from ..core.models import Token, Pool, Chain, TradeAction, BotConfig
from ..core.database import Database

logger = logging.getLogger(__name__)


@dataclass
class BSCPoolInfo:
    """Pool info from PancakeSwap"""
    address: str
    token0: str
    token1: str
    reserve0: int
    reserve1: int
    created: int


class BSCAdapter:
    """
    BSC blockchain adapter.
    Handles RPC calls, PancakeSwap interactions, and wallet tracking.
    """
    
    # Chain ID
    CHAIN_ID = 56
    
    def __init__(self, config: BotConfig, db: Database):
        self.config = config
        self.db = db
        
        # RPC client
        self.rpc_url = config.bsc_rpc or "https://bsc-dataseed.binance.org/"
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        
        # Contracts
        self.router_contract: Optional[Contract] = None
        self.factory_contract: Optional[Contract] = None
        
        # Token cache
        self._token_cache: Dict[str, Dict] = {}
        
        # Known pools
        self._known_pools: set = set()
        
        # Callbacks
        self._on_new_pool: Optional[Callable] = None
        self._on_wallet_activity: Optional[Callable] = None
        
        # Running state
        self._running = False
        self._tasks: List[asyncio.Task] = []
        
        # BEP20 ABI (minimal)
        self._bep20_abi = [
            {
                "inputs": [],
                "name": "symbol",
                "outputs": [{"type": "string"}],
                "stateMutability": "view",
                "type": "function"
            },
            {
                "inputs": [],
                "name": "name",
                "outputs": [{"type": "string"}],
                "stateMutability": "view",
                "type": "function"
            },
            {
                "inputs": [],
                "name": "decimals",
                "outputs": [{"type": "uint8"}],
                "stateMutability": "view",
                "type": "function"
            },
            {
                "inputs": [],
                "name": "totalSupply",
                "outputs": [{"type": "uint256"}],
                "stateMutability": "view",
                "type": "function"
            }
        ]
        
        # Initialize contracts
        self._init_contracts()
    
    def _init_contracts(self):
        """Initialize PancakeSwap contracts"""
        try:
            self.router_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(PANCAKE_SWAP_ROUTER),
                abi=self._get_router_abi()
            )
            
            self.factory_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(PANCAKE_SWAP_FACTORY),
                abi=self._get_factory_abi()
            )
            
            logger.info("BSC contracts initialized")
        except Exception as e:
            logger.error(f"Contract init error: {e}")
    
    def _get_router_abi(self) -> List:
        """PancakeSwap Router v2 ABI (minimal)"""
        return [
            {
                "inputs": [
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMin", "type": "uint256"},
                    {"name": "path", "type": "address[]"},
                    {"name": "to", "type": "address"},
                    {"name": "deadline", "type": "uint256"}
                ],
                "name": "swapExactETHForTokens",
                "outputs": [{"type": "uint256[]"}],
                "stateMutability": "payable",
                "type": "function"
            },
            {
                "inputs": [
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMin", "type": "uint256"},
                    {"name": "path", "type": "address[]"},
                    {"name": "to", "type": "address"},
                    {"name": "deadline", "type": "uint256"}
                ],
                "name": "swapExactTokensForETH",
                "outputs": [{"type": "uint256[]"}],
                "stateMutability": "nonpayable",
                "type": "function"
            },
            {
                "inputs": [
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMin", "type": "uint256"},
                    {"name": "path", "type": "address[]"},
                    {"name": "to", "type": "address"},
                    {"name": "deadline", "type": "uint256"}
                ],
                "name": "swapExactTokensForTokens",
                "outputs": [{"type": "uint256[]"}],
                "stateMutability": "nonpayable",
                "type": "function"
            },
            {
                "inputs": [
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "path", "type": "address[]"}
                ],
                "name": "getAmountsOut",
                "outputs": [{"type": "uint256[]"}],
                "stateMutability": "view",
                "type": "function"
            }
        ]
    
    def _get_factory_abi(self) -> List:
        """PancakeSwap Factory ABI (minimal)"""
        return [
            {
                "inputs": [
                    {"name": "tokenA", "type": "address"},
                    {"name": "tokenB", "type": "address"}
                ],
                "name": "getPair",
                "outputs": [{"type": "address"}],
                "stateMutability": "view",
                "type": "function"
            },
            {
                "inputs": [{"name": "", "type": "address"}],
                "name": "allPairs",
                "outputs": [{"type": "address"}],
                "stateMutability": "view",
                "type": "function"
            },
            {
                "inputs": [],
                "name": "allPairsLength",
                "outputs": [{"type": "uint256"}],
                "stateMutability": "view",
                "type": "function"
            }
        ]
    
    def set_callbacks(
        self,
        on_new_pool: Optional[Callable] = None,
        on_wallet_activity: Optional[Callable] = None
    ):
        """Set callbacks for events"""
        self._on_new_pool = on_new_pool
        self._on_wallet_activity = on_wallet_activity
    
    async def start(self):
        """Start the BSC adapter"""
        self._running = True
        
        # Start pool monitoring
        pool_monitor = asyncio.create_task(self._monitor_new_pools())
        self._tasks.append(pool_monitor)
        
        # Start wallet monitoring
        if self.config.wallet_address:
            wallet_monitor = asyncio.create_task(self._monitor_wallet())
            self._tasks.append(wallet_monitor)
        
        logger.info("BSCAdapter started")
    
    async def stop(self):
        """Stop the BSC adapter"""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("BSCAdapter stopped")
    
    # ============ POOL DISCOVERY ============
    
    async def _monitor_new_pools(self):
        """Monitor for new pools on PancakeSwap"""
        last_checked_pair = 0
        
        while self._running:
            try:
                if not self.factory_contract:
                    await asyncio.sleep(30)
                    continue
                
                # Get total pairs count
                total_pairs = self.factory_contract.functions.allPairsLength().call()
                
                # Check a batch of new pairs
                batch_size = 100
                start = max(last_checked_pair, total_pairs - batch_size)
                
                for i in range(start, total_pairs):
                    pair_addr = self.factory_contract.functions.allPairs(i).call()
                    
                    if pair_addr in self._known_pools:
                        continue
                    
                    self._known_pools.add(pair_addr)
                    
                    # Get pair info
                    pool_info = await self._get_pair_info(pair_addr)
                    if pool_info:
                        await self._process_new_pool(pool_info)
                
                last_checked_pair = total_pairs
                
                await asyncio.sleep(15)  # Check every 15 seconds
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Pool monitoring error: {e}")
                await asyncio.sleep(30)
    
    async def _get_pair_info(self, pair_address: str) -> Optional[Dict]:
        """Get detailed info about a liquidity pair"""
        try:
            pair_address = Web3.to_checksum_address(pair_address)
            
            # Pair contract ABI (minimal)
            pair_abi = [
                {
                    "inputs": [],
                    "name": "getReserves",
                    "outputs": [
                        {"type": "uint112"},
                        {"type": "uint112"},
                        {"type": "uint32"}
                    ],
                    "stateMutability": "view",
                    "type": "function"
                },
                {
                    "inputs": [],
                    "name": "token0",
                    "outputs": [{"type": "address"}],
                    "stateMutability": "view",
                    "type": "function"
                },
                {
                    "inputs": [],
                    "name": "token1",
                    "outputs": [{"type": "address"}],
                    "stateMutability": "view",
                    "type": "function"
                },
                {
                    "inputs": [],
                    "name": "created",
                    "outputs": [{"type": "uint256"}],
                    "stateMutability": "view",
                    "type": "function"
                }
            ]
            
            pair_contract = self.w3.eth.contract(address=pair_address, abi=pair_abi)
            
            token0 = pair_contract.functions.token0().call()
            token1 = pair_contract.functions.token1().call()
            reserves = pair_contract.functions.getReserves().call()
            
            return {
                'address': pair_address,
                'token0': token0,
                'token1': token1,
                'reserve0': reserves[0],
                'reserve1': reserves[1]
            }
            
        except Exception as e:
            logger.debug(f"Pair info error for {pair_address[:8]}: {e}")
            return None
    
    async def _process_new_pool(self, pool_info: Dict):
        """Process a newly discovered pool"""
        token0 = pool_info['token0']
        token1 = pool_info['token1']
        
        # Determine which is the meme token (not WBNB/BUSD)
        meme_token = None
        quote_token = None
        
        if token0.lower() == WBNB.lower():
            quote_token = WBNB
            meme_token = token1
        elif token1.lower() == WBNB.lower():
            quote_token = WBNB
            meme_token = token0
        elif token0.lower() == BUSD.lower():
            quote_token = BUSD
            meme_token = token1
        elif token1.lower() == BUSD.lower():
            quote_token = BUSD
            meme_token = token0
        
        if not meme_token or meme_token in [WBNB, BUSD]:
            return
        
        # Get token info
        token_data = await self._get_token_info(meme_token)
        if not token_data:
            return
        
        # Calculate USD values
        quote_price = await self._get_quote_token_price(quote_token)
        meme_price = await self._get_meme_token_price(pool_info, meme_token, quote_token, quote_price)
        
        reserve_quote = pool_info['reserve0'] if token0.lower() == quote_token.lower() else pool_info['reserve1']
        reserve_meme = pool_info['reserve1'] if token1.lower() == meme_token.lower() else pool_info['reserve0']
        
        # Get token decimals
        decimals = token_data.get('decimals', 18)
        reserve_quote_usd = (reserve_quote / 1e18) * quote_price if quote_token == WBNB else (reserve_quote / 1e18) * 1.0
        
        pool_data = {
            'reserve_usd': reserve_quote_usd,
            'reserve_token': reserve_meme / (10 ** decimals),
            'reserve_quote': reserve_quote / 1e18,
            'volume_24h': 0,  # Would need events for this
            'price': meme_price,
            'creator': ''  # Would need creation tx for this
        }
        
        token = Token(
            symbol=token_data.get('symbol', meme_token[:8]),
            name=token_data.get('name', 'Unknown'),
            address=meme_token,
            chain=Chain.BSC,
            decimals=decimals,
            logo_url=None
        )
        
        if self._on_new_pool:
            await self._on_new_pool(pool_info['address'], token, pool_data)
    
    async def _get_token_info(self, token_address: str) -> Optional[Dict]:
        """Get BEP20 token information"""
        if token_address.lower() in [WBNB.lower(), BUSD.lower()]:
            return {'symbol': 'WBNB', 'name': 'Wrapped BNB', 'decimals': 18}
        
        if token_address in self._token_cache:
            return self._token_cache[token_address]
        
        try:
            addr = Web3.to_checksum_address(token_address)
            contract = self.w3.eth.contract(address=addr, abi=self._bep20_abi)
            
            symbol = contract.functions.symbol().call()
            name = contract.functions.name().call()
            decimals = contract.functions.decimals().call()
            
            info = {
                'symbol': symbol,
                'name': name,
                'decimals': decimals
            }
            
            self._token_cache[token_address] = info
            return info
            
        except Exception as e:
            logger.debug(f"Token info error for {token_address[:8]}: {e}")
            return None
    
    async def _get_quote_token_price(self, quote_token: str) -> float:
        """Get quote token price in USD (BNB ~ $600, BUSD = $1)"""
        if quote_token.lower() == BUSD.lower():
            return 1.0
        elif quote_token.lower() == WBNB.lower():
            # Get BNB price from PancakeSwap
            try:
                if self.router_contract:
                    path = [WBNB, BUSD]
                    amounts = self.router_contract.functions.getAmountsOut(
                        Web3.to_wei(1, 'ether')
                    ).call()
                    return float(amounts[1] / 1e18)
            except:
                pass
            return 600.0  # Fallback
        return 1.0
    
    async def _get_meme_token_price(
        self, 
        pool_info: Dict, 
        meme_token: str, 
        quote_token: str,
        quote_price: float
    ) -> float:
        """Calculate meme token price from reserves"""
        try:
            reserve_quote = pool_info['reserve0'] if pool_info['token0'].lower() == quote_token.lower() else pool_info['reserve1']
            reserve_meme = pool_info['reserve1'] if pool_info['token1'].lower() == meme_token.lower() else pool_info['reserve0']
            
            meme_decimals = (await self._get_token_info(meme_token) or {}).get('decimals', 18)
            
            if reserve_meme == 0:
                return 0.0
            
            price = (reserve_quote / 1e18) * quote_price / (reserve_meme / (10 ** meme_decimals))
            return price
        except:
            return 0.0
    
    # ============ WALLET MONITORING ============
    
    async def _monitor_wallet(self):
        """Monitor wallet for activity"""
        if not self.config.wallet_address:
            return
        
        wallet = Web3.to_checksum_address(self.config.wallet_address)
        last_block = self.w3.eth.block_number
        
        while self._running:
            try:
                current_block = self.w3.eth.block_number
                
                # Process new blocks
                for block_num in range(last_block + 1, current_block + 1):
                    block = self.w3.eth.get_block(block_num, full_transactions=True)
                    
                    for tx in block.transactions:
                        if tx['to'] and tx['to'].lower() == PANCACHE_SWAP_ROUTER.lower():
                            await self._process_swap_tx(tx)
                
                last_block = current_block
                await asyncio.sleep(3)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Wallet monitoring error: {e}")
                await asyncio.sleep(30)
    
    async def _process_swap_tx(self, tx: Dict):
        """Process a swap transaction"""
        try:
            # Parse swap events
            # Simplified - real implementation would decode events
            
            if self._on_wallet_activity:
                self._on_wallet_activity(
                    wallet_address=tx['from'],
                    token_address="unknown",
                    token_symbol="UNKNOWN",
                    action=TradeAction.BUY,
                    amount=float(tx['value'] / 1e18),
                    price=0
                )
        except Exception as e:
            logger.debug(f"Swap tx processing error: {e}")
    
    async def get_wallet_stats(self, wallet_address: str) -> Dict[str, Any]:
        """Get trading stats for a wallet"""
        try:
            addr = Web3.to_checksum_address(wallet_address)
            
            # Count transactions to router
            tx_count = self.w3.eth.transaction_count(addr)
            
            return {
                'total_trades': tx_count,
                'win_rate': 0,
                'avg_trade_size': 0,
                'pnl_30d': 0,
                'last_activity': datetime.utcnow()
            }
        except Exception as e:
            logger.error(f"Wallet stats error: {e}")
            return {}
    
    async def get_wallet_balances(self, wallet_address: str) -> Dict[str, float]:
        """Get all token balances for a wallet"""
        balances = {}
        
        try:
            addr = Web3.to_checksum_address(wallet_address)
            
            # BNB balance
            bnb_balance = self.w3.eth.get_balance(addr)
            balances[WBNB] = float(bnb_balance / 1e18)
            
            # ERC20 / BEP20 token balances - would need to scan logs
            # Simplified: just return BNB for now
            
        except Exception as e:
            logger.error(f"Balance fetch error: {e}")
        
        return balances
    
    # ============ TRADING ============
    
    async def execute_buy(
        self,
        token_address: str,
        amount: float,  # In BNB
        slippage: float = 5.0
    ) -> Dict[str, Any]:
        """Execute a buy order via PancakeSwap"""
        try:
            if not self.router_contract:
                return {'success': False, 'error': 'Router not available'}
            
            if not self.config.wallet_private_key:
                return {'success': False, 'error': 'Wallet key not configured'}
            
            token_addr = Web3.to_checksum_address(token_address)
            
            # Build swap path: WBNB -> Token
            path = [WBNB, token_addr]
            
            # Get amounts
            amount_in = int(amount * 1e18)
            amounts = self.router_contract.functions.getAmountsOut(amount_in, path).call()
            amount_out_min = int(amounts[1] * (1 - slippage / 100))
            
            # Build transaction
            nonce = self.w3.eth.get_transaction_count(
                Web3.to_checksum_address(self.config.wallet_address)
            )
            
            swap_fn = self.router_contract.functions.swapExactETHForTokens(
                amount_out_min,
                path,
                Web3.to_checksum_address(self.config.wallet_address),
                int(datetime.utcnow().timestamp()) + 1200  # 20 min deadline
            )
            
            tx = swap_fn.build_transaction({
                'from': Web3.to_checksum_address(self.config.wallet_address),
                'value': amount_in,
                'gas': 500000,
                'gasPrice': self.w3.eth.gas_price,
                'nonce': nonce,
                'chainId': self.CHAIN_ID
            })
            
            # Sign and send
            signed = self.w3.eth.account.sign_transaction(tx, self.config.wallet_private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
            
            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            if receipt.status == 1:
                return {
                    'success': True,
                    'tx_hash': tx_hash.hex(),
                    'amount_out': amounts[1] / 1e18,
                    'price': amount / (amounts[1] / 1e18),
                    'value_usd': amount * 600  # BNB price
                }
            else:
                return {'success': False, 'error': 'Transaction failed'}
            
        except Exception as e:
            logger.error(f"Buy execution error: {e}")
            return {'success': False, 'error': str(e)}
    
    async def execute_sell(
        self,
        token_address: str,
        quantity: float,
        slippage: float = 5.0
    ) -> Dict[str, Any]:
        """Execute a sell order"""
        try:
            if not self.router_contract:
                return {'success': False, 'error': 'Router not available'}
            
            if not self.config.wallet_private_key:
                return {'success': False, 'error': 'Wallet key not configured'}
            
            token_addr = Web3.to_checksum_address(token_address)
            token_decimals = (await self._get_token_info(token_addr) or {}).get('decimals', 18)
            
            # Build swap path: Token -> WBNB
            path = [token_addr, WBNB]
            
            # Get amounts
            amount_in = int(quantity * (10 ** token_decimals))
            amounts = self.router_contract.functions.getAmountsOut(amount_in, path).call()
            amount_out_min = int(amounts[1] * (1 - slippage / 100))
            
            # Approve token first (if needed)
            # ... (approve logic)
            
            # Build transaction
            nonce = self.w3.eth.get_transaction_count(
                Web3.to_checksum_address(self.config.wallet_address)
            )
            
            swap_fn = self.router_contract.functions.swapExactTokensForETH(
                amount_in,
                amount_out_min,
                path,
                Web3.to_checksum_address(self.config.wallet_address),
                int(datetime.utcnow().timestamp()) + 1200
            )
            
            tx = swap_fn.build_transaction({
                'from': Web3.to_checksum_address(self.config.wallet_address),
                'gas': 500000,
                'gasPrice': self.w3.eth.gas_price,
                'nonce': nonce,
                'chainId': self.CHAIN_ID
            })
            
            # Sign and send
            signed = self.w3.eth.account.sign_transaction(tx, self.config.wallet_private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
            
            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            if receipt.status == 1:
                return {
                    'success': True,
                    'tx_hash': tx_hash.hex(),
                    'amount_out': amounts[1] / 1e18,
                    'price': (amounts[1] / 1e18) / quantity,
                    'value_usd': (amounts[1] / 1e18) * 600
                }
            else:
                return {'success': False, 'error': 'Transaction failed'}
            
        except Exception as e:
            logger.error(f"Sell execution error: {e}")
            return {'success': False, 'error': str(e)}
    
    async def get_token_price(self, token_address: str) -> float:
        """Get current token price in USD"""
        try:
            if self.router_contract:
                path = [Web3.to_checksum_address(token_address), WBNB, BUSD]
                amounts = self.router_contract.functions.getAmountsOut(
                    1 * 10**18
                ).call()
                
                # Price in BNB * BNB/USD
                bnb_price = await self._get_quote_token_price(WBNB)
                return float(amounts[-1] / 1e18) * bnb_price
        except:
            pass
        return 0.0
    
    def get_chain(self) -> Chain:
        """Return the chain type"""
        return Chain.BSC
