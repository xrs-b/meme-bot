#!/usr/bin/env python3
"""
Web Dashboard for Meme Bot
Flask-based monitoring interface
"""

import asyncio
import json
import threading
from datetime import datetime
from typing import Dict, List, Optional, Any
from functools import wraps
import logging

from flask import Flask, render_template, jsonify, request
from flask_cors import CORS

logger = logging.getLogger(__name__)


class WebDashboard:
    """
    Web-based monitoring dashboard for Meme Bot.
    Provides real-time stats, charts, and control panel.
    """
    
    def __init__(self, bot, host: str = "0.0.0.0", port: int = 8080):
        self.bot = bot
        self.host = host
        self.port = port
        
        # Flask app
        self.app = Flask(
            __name__,
            template_folder='templates',
            static_folder='static'
        )
        CORS(self.app)
        
        self.app.config['SECRET_KEY'] = 'meme-bot-dashboard-secret'
        
        # Setup routes
        self._setup_routes()
        
        # Background thread
        self._server_thread: Optional[threading.Thread] = None
        self._running = False
    
    def _setup_routes(self):
        """Setup Flask routes"""
        
        @self.app.route('/')
        def index():
            """Main dashboard page"""
            return render_template('dashboard.html')
        
        @self.app.route('/api/status')
        def api_status():
            """Get overall bot status"""
            return jsonify({
                'status': 'running' if self.bot._running else 'stopped',
                'uptime': 'N/A',  # Would track actual uptime
                'chains': {
                    'solana': Chain.SOLANA in self.bot._adapters,
                    'bsc': Chain.BSC in self.bot._adapters
                },
                'mode': self.bot.config.alert_mode.value,
                'copy_trading': self.bot.copy_trader.enabled
            })
        
        @self.app.route('/api/signals/<chain>')
        def api_signals(chain):
            """Get recent signals for a chain"""
            try:
                chain_enum = Chain.SOLANA if chain == 'sol' else Chain.BSC
                hours = int(request.args.get('hours', 24))
                signals = self.bot.signal_detector.get_recent_signals(chain_enum, hours)
                
                return jsonify({
                    'signals': [
                        {
                            'id': s.id,
                            'type': s.type.value,
                            'symbol': s.token.symbol,
                            'name': s.token.name,
                            'address': s.token.address,
                            'score': s.score,
                            'confidence': s.confidence,
                            'price': s.price_at_signal,
                            'liquidity': s.liquidity_at_signal,
                            'volume': s.volume_24h_at_signal,
                            'detected_at': s.detected_at.isoformat()
                        }
                        for s in signals[:50]  # Limit to 50
                    ],
                    'count': len(signals)
                })
            except Exception as e:
                return jsonify({'error': str(e)}), 400
        
        @self.app.route('/api/positions/<chain>')
        def api_positions(chain):
            """Get positions for a chain"""
            try:
                chain_enum = Chain.SOLANA if chain == 'sol' else Chain.BSC
                positions = self.bot.trading_engine.get_positions(chain_enum)
                
                return jsonify({
                    'positions': [
                        {
                            'symbol': p.token_symbol,
                            'address': p.token_address,
                            'quantity': p.quantity,
                            'avg_price': p.avg_buy_price,
                            'current_price': p.current_price,
                            'value': p.value_now,
                            'pnl_percent': p.pnl_percent,
                            'pnl_usd': p.pnl_usd,
                            'opened_at': p.opened_at.isoformat()
                        }
                        for p in positions
                    ],
                    'count': len(positions),
                    'total_value': sum(p.value_now for p in positions),
                    'total_pnl': sum(p.pnl_usd for p in positions)
                })
            except Exception as e:
                return jsonify({'error': str(e)}), 400
        
        @self.app.route('/api/stats/<chain>')
        def api_stats(chain):
            """Get stats for a chain"""
            try:
                chain_enum = Chain.SOLANA if chain == 'sol' else Chain.BSC
                stats = self.bot.signal_detector.get_signal_stats(chain_enum)
                risk_status = self.bot.risk_manager.get_risk_status() if hasattr(self.bot, 'risk_manager') else None
                
                return jsonify({
                    'stats': stats,
                    'risk': {
                        'total_exposure': risk_status.total_exposure if risk_status else 0,
                        'daily_pnl': risk_status.daily_pnl if risk_status else 0,
                        'positions_at_risk': risk_status.positions_at_risk if risk_status else 0
                    } if risk_status else None
                })
            except Exception as e:
                return jsonify({'error': str(e)}), 400
        
        @self.app.route('/api/trades/<chain>')
        def api_trades(chain):
            """Get recent trades"""
            try:
                chain_enum = Chain.SOLANA if chain == 'sol' else Chain.BSC
                trades = self.bot.db.get_recent_trades(chain_enum, hours=int(request.args.get('hours', 24)))
                
                return jsonify({
                    'trades': [
                        {
                            'id': t.id,
                            'action': t.action.value,
                            'symbol': t.token_symbol,
                            'amount_in': t.amount_in,
                            'amount_out': t.amount_out,
                            'price': t.price,
                            'value': t.value_usd,
                            'pnl_percent': t.pnl_percent,
                            'pnl_usd': t.pnl_usd,
                            'tx_hash': t.tx_hash,
                            'executed_at': t.executed_at.isoformat()
                        }
                        for t in trades[:50]
                    ],
                    'count': len(trades)
                })
            except Exception as e:
                return jsonify({'error': str(e)}), 400
        
        @self.app.route('/api/risk/rules')
        def api_risk_rules():
            """Get risk rules"""
            if not hasattr(self.bot, 'risk_manager'):
                return jsonify({'error': 'Risk manager not enabled'}), 400
            
            rules = self.bot.risk_manager.get_rules()
            return jsonify({
                'rules': {
                    name: {
                        'enabled': rule.enabled,
                        'name': rule.name,
                        **{k: v for k, v in vars(rule).items() if k not in ['name', 'enabled']}
                    }
                    for name, rule in rules.items()
                }
            })
        
        @self.app.route('/api/copy/trading')
        def api_copy_trading():
            """Get copy trading status"""
            stats = self.bot.copy_trader.get_stats()
            return jsonify(stats)
        
        # Control endpoints
        
        @self.app.route('/api/control/mode', methods=['POST'])
        def api_set_mode():
            """Set trading mode"""
            data = request.get_json()
            mode = data.get('mode', 'notify')
            
            if mode == 'auto':
                self.bot.config.alert_mode = AlertMode.AUTO_TRADE
            else:
                self.bot.config.alert_mode = AlertMode.NOTIFY_ONLY
            
            return jsonify({'success': True, 'mode': self.bot.config.alert_mode.value})
        
        @self.app.route('/api/control/liquidate/<chain>', methods=['POST'])
        def api_liquidate(chain):
            """Liquidate all positions"""
            try:
                chain_enum = Chain.SOLANA if chain == 'sol' else Chain.BSC
                
                # Run async task
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                trades = loop.run_until_complete(self.bot.trading_engine.liquidate_all(chain_enum))
                loop.close()
                
                return jsonify({
                    'success': True,
                    'liquidated_count': len(trades),
                    'trades': [{'symbol': t.token_symbol, 'pnl': t.pnl_percent} for t in trades]
                })
            except Exception as e:
                return jsonify({'error': str(e)}), 400
        
        @self.app.route('/api/control/copy', methods=['POST'])
        def api_toggle_copy():
            """Toggle copy trading"""
            data = request.get_json()
            enabled = data.get('enabled', True)
            
            if enabled:
                self.bot.copy_trader.enable()
            else:
                self.bot.copy_trader.disable()
            
            return jsonify({'success': True, 'copy_trading': self.bot.copy_trader.enabled})
        
        @self.app.route('/api/control/risk/<rule_name>', methods=['POST'])
        def api_update_risk(rule_name):
            """Update a risk rule"""
            if not hasattr(self.bot, 'risk_manager'):
                return jsonify({'error': 'Risk manager not enabled'}), 400
            
            data = request.get_json()
            self.bot.risk_manager.update_rule(rule_name, **data)
            
            return jsonify({'success': True})
    
    def start(self):
        """Start the web dashboard in a background thread"""
        if self._running:
            return
        
        self._running = True
        self._server_thread = threading.Thread(target=self._run_server, daemon=True)
        self._server_thread.start()
        logger.info(f"Web dashboard started on http://{self.host}:{self.port}")
    
    def _run_server(self):
        """Run the Flask server"""
        self.app.run(host=self.host, port=self.port, debug=False, use_reloader=False)
    
    def stop(self):
        """Stop the web dashboard"""
        self._running = False
        # Flask doesn't have a clean shutdown, thread will die when process exits
        logger.info("Web dashboard stopped")


# Simple HTML template for the dashboard
DASHBOARD_TEMPLATE = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Meme Bot Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        .card { background: #1a1a2e; border-radius: 12px; padding: 20px; margin: 10px; }
        .positive { color: #10b981; }
        .negative { color: #ef4444; }
        .nav-link { padding: 10px 20px; border-radius: 8px; cursor: pointer; }
        .nav-link.active { background: #374151; }
        .tab { display: none; }
        .tab.active { display: block; }
    </style>
</head>
<body class="bg-gray-900 text-gray-100 min-h-screen">
    <nav class="bg-gray-800 border-b border-gray-700 p-4">
        <div class="flex items-center justify-between max-w-7xl mx-auto">
            <h1 class="text-2xl font-bold text-purple-400">🐸 Meme Bot</h1>
            <div class="flex gap-2">
                <button onclick="setMode('notify')" id="btn-notify" class="nav-link bg-purple-600">🔔 观察</button>
                <button onclick="setMode('auto')" id="btn-auto" class="nav-link bg-gray-700">🤖 自动</button>
            </div>
        </div>
    </nav>
    
    <main class="max-w-7xl mx-auto p-4">
        <!-- Status Cards -->
        <div class="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
            <div class="card">
                <div class="text-gray-400 text-sm">总信号</div>
                <div class="text-3xl font-bold" id="total-signals">-</div>
            </div>
            <div class="card">
                <div class="text-gray-400 text-sm">总交易</div>
                <div class="text-3xl font-bold" id="total-trades">-</div>
            </div>
            <div class="card">
                <div class="text-gray-400 text-sm">胜率</div>
                <div class="text-3xl font-bold" id="win-rate">-</div>
            </div>
            <div class="card">
                <div class="text-gray-400 text-sm">总 P&L</div>
                <div class="text-3xl font-bold" id="total-pnl">-</div>
            </div>
        </div>
        
        <!-- Chain Tabs -->
        <div class="flex gap-2 mb-4">
            <button onclick="showChain('sol')" class="nav-link active" id="tab-sol">🟣 Solana</button>
            <button onclick="showChain('bsc')" class="nav-link" id="tab-bsc">🟠 BSC</button>
        </div>
        
        <!-- Chain Content -->
        <div id="content-sol" class="tab active">
            <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
                <div class="card">
                    <div class="text-gray-400 text-sm">SOL 流动性</div>
                    <div class="text-2xl font-bold">$<span id="sol-liquidity">-</span></div>
                </div>
                <div class="card">
                    <div class="text-gray-400 text-sm">SOL 信号</div>
                    <div class="text-2xl font-bold" id="sol-signals">-</div>
                </div>
                <div class="card">
                    <div class="text-gray-400 text-sm">SOL 持仓</div>
                    <div class="text-2xl font-bold" id="sol-positions">-</div>
                </div>
            </div>
            <div class="card">
                <h3 class="text-lg font-bold mb-4">最近信号</h3>
                <div id="sol-signals-table" class="overflow-x-auto"></div>
            </div>
        </div>
        
        <div id="content-bsc" class="tab">
            <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
                <div class="card">
                    <div class="text-gray-400 text-sm">BSC 流动性</div>
                    <div class="text-2xl font-bold">$<span id="bsc-liquidity">-</span></div>
                </div>
                <div class="card">
                    <div class="text-gray-400 text-sm">BSC 信号</div>
                    <div class="text-2xl font-bold" id="bsc-signals">-</div>
                </div>
                <div class="card">
                    <div class="text-gray-400 text-sm">BSC 持仓</div>
                    <div class="text-2xl font-bold" id="bsc-positions">-</div>
                </div>
            </div>
            <div class="card">
                <h3 class="text-lg font-bold mb-4">最近信号</h3>
                <div id="bsc-signals-table" class="overflow-x-auto"></div>
            </div>
        </div>
        
        <!-- Positions Section -->
        <div class="card mt-6">
            <div class="flex justify-between items-center mb-4">
                <h3 class="text-lg font-bold">当前持仓</h3>
                <button onclick="liquidateAll()" class="bg-red-600 hover:bg-red-700 px-4 py-2 rounded">一键清仓</button>
            </div>
            <div id="positions-table" class="overflow-x-auto"></div>
        </div>
        
        <!-- Risk Rules -->
        <div class="card mt-6">
            <h3 class="text-lg font-bold mb-4">风控规则</h3>
            <div id="risk-rules" class="grid grid-cols-2 md:grid-cols-4 gap-4"></div>
        </div>
    </main>
    
    <script>
        let currentChain = 'sol';
        
        async function fetchJSON(url) {
            const resp = await fetch(url);
            return resp.json();
        }
        
        async function setMode(mode) {
            await fetch('/api/control/mode', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({mode})
            });
            document.getElementById('btn-notify').className = 'nav-link ' + (mode === 'notify' ? 'bg-purple-600' : 'bg-gray-700');
            document.getElementById('btn-auto').className = 'nav-link ' + (mode === 'auto' ? 'bg-purple-600' : 'bg-gray-700');
        }
        
        function showChain(chain) {
            currentChain = chain;
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.nav-link').forEach(t => t.classList.remove('active'));
            document.getElementById('content-' + chain).classList.add('active');
            document.getElementById('tab-' + chain).classList.add('active');
            loadData();
        }
        
        async function loadData() {
            // Load SOL data
            const solSignals = await fetchJSON('/api/signals/sol');
            const solStats = await fetchJSON('/api/stats/sol');
            const solPositions = await fetchJSON('/api/positions/sol');
            
            document.getElementById('sol-signals').textContent = solSignals.count || 0;
            document.getElementById('sol-positions').textContent = solPositions.count || 0;
            
            if (solSignals.signals) {
                renderSignalsTable('sol-signals-table', solSignals.signals);
            }
            
            // Load BSC data
            const bscSignals = await fetchJSON('/api/signals/bsc');
            const bscStats = await fetchJSON('/api/stats/bsc');
            const bscPositions = await fetchJSON('/api/positions/bsc');
            
            document.getElementById('bsc-signals').textContent = bscSignals.count || 0;
            document.getElementById('bsc-positions').textContent = bscPositions.count || 0;
            
            if (bscSignals.signals) {
                renderSignalsTable('bsc-signals-table', bscSignals.signals);
            }
            
            // Overall stats
            document.getElementById('total-signals').textContent = (solSignals.count || 0) + (bscSignals.count || 0);
            
            // Positions
            const allPositions = [...(solPositions.positions || []), ...(bscPositions.positions || [])];
            renderPositionsTable(allPositions);
            
            // Risk rules
            loadRiskRules();
        }
        
        function renderSignalsTable(containerId, signals) {
            const container = document.getElementById(containerId);
            if (!signals || signals.length === 0) {
                container.innerHTML = '<p class="text-gray-400">暂无信号</p>';
                return;
            }
            
            let html = '<table class="w-full text-sm"><thead><tr class="text-left text-gray-400">';
            html += '<th>评分</th><th>币种</th><th>类型</th><th>流动性</th><th>交易量</th><th>时间</th></tr></thead><tbody>';
            
            signals.forEach(s => {
                const scoreColor = s.score >= 80 ? 'text-green-400' : s.score >= 60 ? 'text-yellow-400' : 'text-red-400';
                html += '<tr class="border-t border-gray-700">';
                html += `<td class="${scoreColor} font-bold">${s.score}</td>`;
                html += `<td><span class="font-bold">${s.symbol}</span><br><span class="text-gray-400 text-xs">${s.name}</span></td>`;
                html += `<td>${s.type}</td>`;
                html += `<td>$${formatNumber(s.liquidity)}</td>`;
                html += `<td>$${formatNumber(s.volume)}</td>`;
                html += `<td>${new Date(s.detected_at).toLocaleString()}</td>`;
                html += '</tr>';
            });
            
            html += '</tbody></table>';
            container.innerHTML = html;
        }
        
        function renderPositionsTable(positions) {
            const container = document.getElementById('positions-table');
            if (!positions || positions.length === 0) {
                container.innerHTML = '<p class="text-gray-400">暂无持仓</p>';
                return;
            }
            
            let html = '<table class="w-full text-sm"><thead><tr class="text-left text-gray-400">';
            html += '<th>币种</th><th>数量</th><th>均价</th><th>现价</th><th>价值</th><th>P&L</th></tr></thead><tbody>';
            
            positions.forEach(p => {
                const pnlColor = p.pnl_percent >= 0 ? 'text-green-400' : 'text-red-400';
                html += '<tr class="border-t border-gray-700">';
                html += `<td class="font-bold">${p.symbol}</td>`;
                html += `<td>${p.quantity.toFixed(2)}</td>`;
                html += `<td>$${p.avg_price.toFixed(8)}</td>`;
                html += `<td>$${p.current_price.toFixed(8)}</td>`;
                html += `<td>$${p.value.toFixed(2)}</td>`;
                html += `<td class="${pnlColor}">${p.pnl_percent.toFixed(2)}% ($${p.pnl_usd.toFixed(2)})</td>`;
                html += '</tr>';
            });
            
            html += '</tbody></table>';
            container.innerHTML = html;
        }
        
        async function loadRiskRules() {
            try {
                const data = await fetchJSON('/api/risk/rules');
                const container = document.getElementById('risk-rules');
                let html = '';
                
                for (const [name, rule] of Object.entries(data.rules)) {
                    const status = rule.enabled ? '🟢' : '🔴';
                    html += `<div class="bg-gray-800 p-4 rounded-lg">`;
                    html += `<div class="flex justify-between items-center mb-2">`;
                    html += `<span class="font-bold">${rule.name}</span>`;
                    html += `<span>${status}</span>`;
                    html += `</div>`;
                    
                    if (rule.stop_loss_percent) html += `<div class="text-sm text-gray-400">止损: ${rule.stop_loss_percent}%</div>`;
                    if (rule.take_profit_percent) html += `<div class="text-sm text-gray-400">止盈: ${rule.take_profit_percent}%</div>`;
                    if (rule.max_position_size) html += `<div class="text-sm text-gray-400">单笔上限: ${rule.max_position_size}</div>`;
                    if (rule.min_liquidity) html += `<div class="text-sm text-gray-400">最小流动性: $${formatNumber(rule.min_liquidity)}</div>`;
                    
                    html += '</div>';
                }
                
                container.innerHTML = html;
            } catch (e) {
                console.log('Risk rules not available');
            }
        }
        
        async function liquidateAll() {
            if (!confirm('确定要清仓所有持仓吗？')) return;
            
            const chain = currentChain;
            const resp = await fetch(`/api/control/liquidate/${chain}`, {method: 'POST'});
            const data = await resp.json();
            
            alert(`已清仓 ${data.liquidated_count} 个持仓`);
            loadData();
        }
        
        function formatNumber(num) {
            if (num >= 1e9) return (num / 1e9).toFixed(2) + 'B';
            if (num >= 1e6) return (num / 1e6).toFixed(2) + 'M';
            if (num >= 1e3) return (num / 1e3).toFixed(2) + 'K';
            return num.toFixed(2);
        }
        
        // Load data on start
        loadData();
        setInterval(loadData, 30000); // Refresh every 30s
    </script>
</body>
</html>
'''


def create_dashboard_template():
    """Create the dashboard templates directory and save template"""
    import os
    template_dir = os.path.join(os.path.dirname(__file__), 'templates')
    os.makedirs(template_dir, exist_ok=True)
    
    with open(os.path.join(template_dir, 'dashboard.html'), 'w') as f:
        f.write(DASHBOARD_TEMPLATE)
    
    # Create static directory
    static_dir = os.path.join(os.path.dirname(__file__), 'static')
    os.makedirs(static_dir, exist_ok=True)


if __name__ == '__main__':
    # For testing
    create_dashboard_template()
    print("Dashboard template created")
