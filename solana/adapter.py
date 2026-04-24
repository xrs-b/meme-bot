#!/usr/bin/env python3
"""
Solana Chain Adapter for Meme Bot
Handles all Solana blockchain interactions: RPC, DEXes, wallet tracking
"""

import asyncio
import json
import base64
import struct
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass
import logging

import aiohttp
from solana.rpc.api import Client as SolanaClient
from solana.rpc.commitment import Confirmed, Finalized
from solana.rpc.types import TxOpts
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.message import Message
import spl.token.constants as TOKEN_CONSTANTS

# For Jupiter DEX
from jupiter.interface import Jupiter

# For Raydium
from raydium.raydium import Raydium

from ..core.models import Token, Pool, Chain, TradeAction, BotConfig
from ..core.database import Database

logger = logging.getLogger(__name__)


@dataclass
class SolanaTokenInfo:
    """Token info from Solana"""
    mint: str
    symbol: str
    name: str
    decimals: int
    logo_uri: Optional[str] = None


class SolanaAdapter:
    """
    Solana blockchain adapter.
    Handles RPC calls, DEX interactions, and wallet tracking.
    """
    
    # Known DEX program IDs on Solana
    RAYDIUM_AMM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUtSMp8"
    ORCA_WHIRLPOOL = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctoyCc"
    JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3IQLUZqHNGtwMFGg"
    
    # Token addresses
    WRAPPED_SOL = "So11111111111111111111111111111111111111112"
    USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
    
    def __init__(self, config: BotConfig, db: Database):
        self.config = config
        self.db = db
        
        # RPC client
        self.rpc_url = config.solana_rpc or "https://api.mainnet-beta.solana.com"
        self.ws_url = config.solana_ws or self.rpc_url.replace("https://", "wss://")
        self.client = SolanaClient(self.rpc_url)
        
        # DEX clients
        self._jupiter: Optional[Jupiter] = None
        self._raydium: Optional[Raydium] = None
        
        # Wallet to track for copy trading
        self._wallet_pubkey: Optional[Pubkey] = None
        if config.wallet_address:
            try:
                self._wallet_pubkey = Pubkey.from_string(config.wallet_address)
            except:
                logger.error(f"Invalid wallet address: {config.wallet_address}")
        
        # Subscription handles
        self._subscriptions: List[int] = []
        
        # Token cache
        self._token_cache: Dict[str, SolanaTokenInfo] = {}
        
        # Running state
        self._running = False
        self._tasks: List[asyncio.Task] = []
        
        # Callbacks for new pools/signals
        self._on_new_pool: Optional[Callable] = None
        self._on_wallet_activity: Optional[Callable] = None
    
    def set_callbacks(
        self,
        on_new_pool: Optional[Callable] = None,
        on_wallet_activity: Optional[Callable] = None
    ):
        """Set callbacks for events"""
        self._on_new_pool = on_new_pool
        self._on_wallet_activity = on_wallet_activity
    
    async def start(self):
        """Start the Solana adapter"""
        self._running = True
        
        # Start DEX clients
        await self._init_dex_clients()
        
        # Start pool monitoring
        pool_monitor = asyncio.create_task(self._monitor_new_pools())
        self._tasks.append(pool_monitor)
        
        # Start wallet monitoring if configured
        if self._wallet_pubkey:
            wallet_monitor = asyncio.create_task(self._monitor_wallet())
            self._tasks.append(wallet_monitor)
        
        logger.info("SolanaAdapter started")
    
    async def stop(self):
        """Stop the Solana adapter"""
        self._running = False
        
        # Cancel subscriptions
        for sub_id in self._subscriptions:
            try:
                await self._unsubscribe(sub_id)
            except:
                pass
        
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        
        logger.info("SolanaAdapter stopped")
    
    async def _init_dex_clients(self):
        """Initialize DEX clients"""
        try:
            # Jupiter - for swaps and price
            self._jupiter = Jupiter()
            await self._jupiter.init()
            logger.info("Jupiter client initialized")
        except Exception as e:
            logger.warning(f"Jupiter init failed: {e}")
        
        try:
            # Raydium - for pool discovery
            self._raydium = Raydium()
            await self._raydium.init()
            logger.info("Raydium client initialized")
        except Exception as e:
            logger.warning(f"Raydium init failed: {e}")
    
    # ============ POOL DISCOVERY ============
    
    async def _monitor_new_pools(self):
        """Monitor for new pools on Raydium/Jupiter"""
        known_pools = set()
        
        while self._running:
            try:
                # Get recent pools from Raydium
                if self._raydium:
                    pools = await self._raydium.get_new_pools()
                    
                    for pool_info in pools:
                        pool_address = pool_info.get('amm_id') or pool_info.get('address')
                        if pool_address and pool_address not in known_pools:
                            known_pools.add(pool_address)
                            
                            # Get token info
                            base_token = pool_info.get('base_mint', '')
                            quote_token = pool_info.get('quote_mint', '')
                            
                            # Skip if not a meme coin (quote should be SOL or USDC)
                            if quote_token not in [self.WRAPPED_SOL, self.USDC, self.USDT]:
                                continue
                            
                            # Get token metadata
                            token_info = await self._get_token_info(base_token)
                            
                            # Build pool data
                            pool_data = {
                                'reserve_usd': pool_info.get('quote_reserve', 0) * self._get_token_price(quote_token),
                                'reserve_token': pool_info.get('base_reserve', 0),
                                'reserve_quote': pool_info.get('quote_reserve', 0),
                                'volume_24h': pool_info.get('volume_24h', 0),
                                'price': pool_info.get('price', 0),
                                'creator': pool_info.get('creator', '')
                            }
                            
                            token = Token(
                                symbol=token_info.symbol if token_info else base_token[:8],
                                name=token_info.name if token_info else "Unknown",
                                address=base_token,
                                chain=Chain.SOLANA,
                                decimals=token_info.decimals if token_info else 9,
                                logo_url=token_info.logo_uri if token_info else None
                            )
                            
                            # Trigger callback
                            if self._on_new_pool:
                                await self._on_new_pool(pool_address, token, pool_data)
                
                # Also check Jupiter for new tokens
                if self._jupiter:
                    jup_tokens = await self._jupiter.get_new_tokens()
                    for token_data in jup_tokens:
                        token_address = token_data.get('address')
                        if token_address and token_address not in known_pools:
                            known_pools.add(token_address)
                            
                            token_info = await self._get_token_info(token_address)
                            if token_info:
                                token = Token(
                                    symbol=token_info.symbol,
                                    name=token_info.name,
                                    address=token_address,
                                    chain=Chain.SOLANA,
                                    decimals=token_info.decimals,
                                    logo_url=token_info.logo_uri
                                )
                                
                                pool_data = {
                                    'reserve_usd': 0,
                                    'reserve_token': 0,
                                    'reserve_quote': 0,
                                    'volume_24h': 0,
                                    'price': 0,
                                    'creator': ''
                                }
                                
                                if self._on_new_pool:
                                    await self._on_new_pool(f"jup_{token_address}", token, pool_data)
                
                await asyncio.sleep(10)  # Check every 10 seconds
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Pool monitoring error: {e}")
                await asyncio.sleep(30)
    
    async def _get_token_info(self, mint: str) -> Optional[SolanaTokenInfo]:
        """Get token info from cache or Solana"""
        if mint in self._token_cache:
            return self._token_cache[mint]
        
        try:
            # Use Solana RPC to get supply info
            resp = await self.client.get_supply(mint)
            if resp.value:
                # Try to get metadata from Jupiter
                if self._jupiter:
                    meta = await self._jupiter.get_token_meta(mint)
                    if meta:
                        info = SolanaTokenInfo(
                            mint=mint,
                            symbol=meta.get('symbol', mint[:8]),
                            name=meta.get('name', 'Unknown'),
                            decimals=resp.value.decimals,
                            logo_uri=meta.get('logo_uri')
                        )
                        self._token_cache[mint] = info
                        return info
                
                # Fallback
                info = SolanaTokenInfo(
                    mint=mint,
                    symbol=mint[:8],
                    name="Unknown",
                    decimals=resp.value.decimals
                )
                self._token_cache[mint] = info
                return info
        except Exception as e:
            logger.debug(f"Token info lookup failed for {mint[:8]}: {e}")
        
        return None
    
    def _get_token_price(self, mint: str) -> float:
        """Get token price in USD (simplified)"""
        if mint == self.WRAPPED_SOL:
            return 100.0  # Should fetch real price
        elif mint in [self.USDC, self.USDT]:
            return 1.0
        return 0.0
    
    # ============ WALLET MONITORING ============
    
    async def _monitor_wallet(self):
        """Monitor wallet for activity (for copy trading)"""
        if not self._wallet_pubkey:
            return
        
        last_signature = None
        
        while self._running:
            try:
                # Get recent transactions
                resp = await self.client.get_signatures_for_address(
                    self._wallet_pubkey,
                    limit=10
                )
                
                signatures = resp.value
                if signatures and signatures[0].signature != last_signature:
                    # New transaction(s) detected
                    for sig_info in signatures[:5]:  # Process up to 5
                        if sig_info.signature == last_signature:
                            break
                        
                        # Get transaction details
                        tx_resp = await self.client.get_transaction(
                            sig_info.signature,
                            max_supported_transaction_version=0
                        )
                        
                        if tx_resp.value:
                            await self._process_wallet_tx(tx_resp.value)
                    
                    last_signature = signatures[0].signature
                
                await asyncio.sleep(5)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Wallet monitoring error: {e}")
                await asyncio.sleep(30)
    
    async def _process_wallet_tx(self, tx_data: Any):
        """Process a wallet transaction"""
        try:
            # Extract token transfers from transaction
            # This is simplified - real implementation would parse instruction data
            
            message = tx_data.transaction.message
            instructions = message.instructions
            
            for ix in instructions:
                # Check if it's a token transfer
                program_id = str(ix.program_id)
                
                if program_id in [self.RAYDIUM_AMM, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"]:
                    # Try to parse the instruction
                    data = ix.data
                    if len(data) > 0:
                        # Simplified - real parsing needed
                        pass
            
            # Alert callback
            if self._on_wallet_activity:
                # Extract relevant info
                self._on_wallet_activity(
                    wallet_address=str(self._wallet_pubkey),
                    token_address="unknown",
                    token_symbol="UNKNOWN",
                    action=TradeAction.BUY,  # Simplified
                    amount=0,
                    price=0
                )
                
        except Exception as e:
            logger.debug(f"Tx processing error: {e}")
    
    async def get_wallet_stats(self, wallet_address: str) -> Dict[str, Any]:
        """Get trading stats for a wallet (for copy trading)"""
        try:
            pubkey = Pubkey.from_string(wallet_address)
            
            # Get recent transactions
            resp = await self.client.get_signatures_for_address(pubkey, limit=100)
            signatures = resp.value
            
            total_trades = len(signatures)
            wins = 0  # Would need price data to determine
            
            return {
                'total_trades': total_trades,
                'win_rate': (wins / total_trades * 100) if total_trades > 0 else 0,
                'avg_trade_size': 0,  # Would need amount parsing
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
            pubkey = Pubkey.from_string(wallet_address)
            
            # Get SOL balance
            sol_resp = await self.client.get_balance(pubkey)
            if sol_resp.value:
                balances[self.WRAPPED_SOL] = sol_resp.value / 1e9  # Lamports to SOL
            
            # Get SPL token balances
            token_resp = await self.client.get_token_accounts_by_owner(
                pubkey,
                {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}
            )
            
            for account in token_resp.value:
                try:
                    data = account.account.data.parsed
                    mint = data['info']['mint']
                    amount = int(data['info']['tokenAmount']['amount'])
                    decimals = data['info']['tokenAmount']['decimals']
                    
                    # Convert to readable amount
                    balance = amount / (10 ** decimals)
                    if balance > 0:
                        balances[mint] = balance
                except:
                    pass
                    
        except Exception as e:
            logger.error(f"Balance fetch error: {e}")
        
        return balances
    
    # ============ TRADING ============
    
    async def execute_buy(
        self,
        token_address: str,
        amount: float,  # In SOL
        slippage: float = 5.0
    ) -> Dict[str, Any]:
        """Execute a buy order via Jupiter/Raydium"""
        try:
            # Convert SOL to WSOL
            wsol_address = self.WRAPPED_SOL
            
            # Get Jupiter quote for the swap
            if not self._jupiter:
                logger.error("Jupiter not initialized")
                return {'success': False, 'error': 'DEX not available'}
            
            # Get quote
            quote = await self._jupiter.quote({
                'inputMint': wsol_address,
                'outputMint': token_address,
                'amount': int(amount * 1e9),  # Lamports
                'slippage': slippage
            })
            
            if not quote:
                return {'success': False, 'error': 'No quote available'}
            
            # Execute swap
            result = await self._jupiter.swap(quote)
            
            if result and result.get('success'):
                return {
                    'success': True,
                    'tx_hash': result.get('txId', ''),
                    'amount_out': result.get('outAmount', 0) / (10 ** 9),
                    'price': amount / (result.get('outAmount', 1) / 1e9),
                    'value_usd': amount * 100  # Simplified
                }
            
            return {'success': False, 'error': 'Swap failed'}
            
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
            wsol_address = self.WRAPPED_SOL
            
            if not self._jupiter:
                logger.error("Jupiter not initialized")
                return {'success': False, 'error': 'DEX not available'}
            
            # Get token decimals
            token_info = await self._get_token_info(token_address)
            decimals = token_info.decimals if token_info else 9
            
            # Get quote
            quote = await self._jupiter.quote({
                'inputMint': token_address,
                'outputMint': wsol_address,
                'amount': int(quantity * (10 ** decimals)),
                'slippage': slippage
            })
            
            if not quote:
                return {'success': False, 'error': 'No quote available'}
            
            # Execute swap
            result = await self._jupiter.swap(quote)
            
            if result and result.get('success'):
                return {
                    'success': True,
                    'tx_hash': result.get('txId', ''),
                    'amount_out': result.get('outAmount', 0) / 1e9,  # WSOL
                    'price': (result.get('outAmount', 0) / 1e9) / quantity,
                    'value_usd': (result.get('outAmount', 0) / 1e9) * 100
                }
            
            return {'success': False, 'error': 'Swap failed'}
            
        except Exception as e:
            logger.error(f"Sell execution error: {e}")
            return {'success': False, 'error': str(e)}
    
    async def get_token_price(self, token_address: str) -> float:
        """Get current token price in USD"""
        try:
            if self._jupiter:
                # Use Jupiter price API
                price = await self._jupiter.get_price(token_address)
                return price.get('USD', 0)
        except:
            pass
        
        return 0.0
    
    # ============ HELPERS ============
    
    async def _subscribe(self, method: str, params: List) -> int:
        """Subscribe to websocket events (simplified)"""
        # In production, use websocket RPC
        return 0
    
    async def _unsubscribe(self, subscription_id: int):
        """Unsubscribe from events"""
        pass
    
    def get_chain(self) -> Chain:
        """Return the chain type"""
        return Chain.SOLANA
