#!/usr/bin/env python3
"""Standalone pump.fun scanner with improved scoring and format"""
import subprocess
import json
import os
import asyncio
import sys
import uuid
import sqlite3
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/root/.openclaw/workspace/meme-bot')

# Trading config for mode detection
from trading.config_manager import ConfigManager, BotMode

DB_PATH = '/root/.openclaw/workspace/meme-bot/meme_bot.db'

def _run_cmd(args):
    env = os.environ.copy()
    env['PATH'] = '/root/.local/bin:' + env.get('PATH', '')
    cmd = ' '.join(args)
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env, timeout=25)
    if result.returncode != 0:
        return {}
    return json.loads(result.stdout)

def _fmt_usd(v):
    """Format USD value"""
    if v <= 0: return "$-"
    if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
    if v >= 1_000: return f"${v/1_000:.0f}K"
    return f"${v:.0f}"

def _fmt_sol(v):
    """Format SOL value"""
    if v <= 0: return "0 SOL"
    if v >= 1000: return f"{v:.0f} SOL"
    if v >= 1: return f"{v:.1f} SOL"
    return f"{v:.3f} SOL"

def _fmt_price(v):
    """Format price without scientific notation"""
    if v <= 0: return "$0"
    if v >= 1: return f"${v:.4f}"
    s = f"{v:.12f}".rstrip('0').rstrip('.')
    if len(s) > 8:
        zeros = 0
        for c in s[2:]:
            if c == '0': zeros += 1
            else: break
        if zeros >= 4:
            return f"$0.0{{{zeros}}}{s[2+zeros:]}"
    return f"${s}"

def _fmt_age(create_time_ms):
    if not create_time_ms: return "未知"
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    age_ms = now_ms - create_time_ms
    if age_ms < 0: return "刚刚"
    total_seconds = age_ms / 1000
    days = int(total_seconds // 86400)
    hours = int((total_seconds % 86400) // 3600)
    minutes = int((total_seconds % 3600) // 60)
    if days > 0: return f"{days}天{hours}小时{minutes}分钟"
    if hours > 0: return f"{hours}小时{minutes}分钟"
    return f"{minutes}分钟"

def _extract_twitter_handle(url):
    """从Twitter URL中提取handle"""
    if not url:
        return None
    # https://x.com/handle/status/... → handle
    parts = url.split('/')
    if len(parts) >= 4:
        return parts[3].split('?')[0]
    return None

def _calc_score(liq_usd, holders, top10, bp, mc, vol_usd=0, dev_holding=0, dev_rugs=0, dev_launched=0, buy_tx=0, sell_tx=0, has_social=False, price_chg5m=0, twitter_handle=None, twitter_reuse_count=0):
    """金狗评分系统 v8 - P2新增指标"""
    score = 0
    
    # 流动性 (max 25)
    if liq_usd >= 50000: score += 25
    elif liq_usd >= 25000: score += 22
    elif liq_usd >= 10000: score += 19
    elif liq_usd >= 5000: score += 16
    elif liq_usd >= 2500: score += 13
    elif liq_usd >= 1000: score += 10
    elif liq_usd >= 500: score += 7
    elif liq_usd >= 100: score += 4
    elif liq_usd >= 50: score += 2
    elif liq_usd > 0: score += 1
    
    # 持币人数 (max 20)
    if holders >= 500: score += 20
    elif holders >= 200: score += 16
    elif holders >= 100: score += 13
    elif holders >= 50: score += 10
    elif holders >= 20: score += 7
    elif holders >= 10: score += 4
    elif holders >= 5: score += 2
    elif holders > 0: score += 1
    
    # 老鼠仓 - top10持仓 (max -20)
    if top10 >= 80: score -= 20
    elif top10 >= 70: score -= 16
    elif top10 >= 60: score -= 12
    elif top10 >= 50: score -= 8
    elif top10 >= 40: score -= 4
    elif top10 >= 30: score -= 2
    
    # Bonding曲线进度 (max 10)
    if bp >= 90: score += 10
    elif bp >= 80: score += 8
    elif bp >= 70: score += 6
    elif bp >= 50: score += 4
    elif bp >= 30: score += 2
    
    # 流动性/市值比 (max 5)
    if mc > 0 and liq_usd > 0:
        ratio = liq_usd / mc
        if ratio >= 1.0: score += 5
        elif ratio >= 0.5: score += 3
        elif ratio >= 0.2: score += 2
        elif ratio >= 0.1: score += 1
    
    # 24h成交量 (max 3)
    if vol_usd >= 10000: score += 3
    elif vol_usd >= 5000: score += 2
    elif vol_usd >= 1000: score += 1
    
    # P0: Dev自己持仓 - 最高扣15分 (最危险的Rug方式)
    if dev_holding >= 20: score -= 15
    elif dev_holding >= 15: score -= 12
    elif dev_holding >= 10: score -= 8
    elif dev_holding >= 6: score -= 5
    elif dev_holding >= 3: score -= 3
    
    # P0: Dev历史Rug记录 - 有前科的开发者更可能继续Rug
    if dev_rugs >= 3: score -= 10
    elif dev_rugs >= 2: score -= 6
    elif dev_rugs >= 1: score -= 3
    
    # P1: 买卖比 (max +5) - 买入远大于卖出 = 强劲需求
    if sell_tx > 0 and buy_tx > 0:
        ratio = buy_tx / sell_tx
        if ratio >= 10: score += 5
        elif ratio >= 5: score += 4
        elif ratio >= 3: score += 3
        elif ratio >= 2: score += 2
        elif ratio > 1: score += 1
    elif buy_tx > 0 and sell_tx == 0:
        # 无人卖出 = 强力吸筹信号
        score += 3
    
    # P1: 社交媒体存在性 (+3) - 有社区运营的币更有机会
    if has_social: score += 3
    
    # P2: 价格动量 (max +5) - 5分钟价格变化率
    if price_chg5m >= 200: score += 5
    elif price_chg5m >= 100: score += 4
    elif price_chg5m >= 50: score += 3
    elif price_chg5m >= 20: score += 2
    elif price_chg5m >= 10: score += 1
    elif price_chg5m <= -50: score -= 3  # 5分钟跌超50% = 砸盘嫌疑
    elif price_chg5m <= -20: score -= 1
    
    # P2: Twitter批量发币检测 - 同一账号发多个币 = 批量发币狗
    if twitter_reuse_count >= 5: score -= 8
    elif twitter_reuse_count >= 3: score -= 5
    elif twitter_reuse_count >= 2: score -= 3
    
    return max(0, min(100, score))

def _get_risk_label(sniper):
    """老鼠仓风险等级基于狙击机器人持仓比例"""
    if sniper >= 50: return "🚨 极度危险"
    elif sniper >= 30: return "⚠️ 危险"
    elif sniper >= 15: return "⚠️ 较危险"
    elif sniper >= 5: return "⚠️ 轻微"
    return "✅ 安全"

def _get_dev_risk_label(dev_holding, dev_rugs, dev_launched=0):
    """Dev持仓风险 + Rug历史 + 连续发币"""
    parts = []
    if dev_holding >= 20: parts.append("🚨 Dev高度控盘")
    elif dev_holding >= 10: parts.append("⚠️ Dev持仓过高")
    elif dev_holding >= 5: parts.append("⚠️ Dev有持仓")
    
    if dev_rugs >= 3: parts.append(f"🚨 历史Rug {dev_rugs}次")
    elif dev_rugs >= 1: parts.append(f"⚠️ 历史Rug {dev_rugs}次")
    
    if dev_launched >= 10: parts.append(f"⚠️ 连续发币{dev_launched}次")
    
    if not parts: return "✅ Dev无异常"
    return " ".join(parts)

def _get_antibot_label(is_internal):
    """Anti-Bot模式检测"""
    return "✅ 公平发射" if is_internal else "⚠️ 机器人可绕过的发射"

def _save_signal(tok, score):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        detected_at = datetime.now(timezone.utc).isoformat()
        metadata = json.dumps({
            'top10_holders': tok['top10'],
            'holders': tok['holders'],
            'bonding': tok['bonding'],
            'chain_index': tok['chain_index'],
            'new_score': score,
            'mc': tok['mc'],
            'pool_sol': tok['pool_sol'],
        })
        cur.execute("""
            INSERT INTO signals (id, type, chain, token_address, message, confidence, score,
                              price_at_signal, liquidity_at_signal, volume_24h_at_signal,
                              source_address, detected_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(uuid.uuid4()), 'new_coin', 'solana', tok['address'],
            f"OKX pump.fun: {tok['symbol']}", score / 100, score,
            tok['price_usd'], tok['pool_usdt'], tok['vol_usdt'],
            'okx_onchainos', detected_at, metadata
        ))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"DB error: {e}")
        return False

def scan_and_send(min_score=20, max_per_scan=3):
    """Scan pump.fun (NEW + MIGRATING) with API-level filtering and send signals"""
    # Get SOL price
    sol_data = _run_cmd(['onchainos', 'token', 'price-info', '--chain', 'solana', '--address', 'So11111111111111111111111111111111111111112'])
    sol_price = float(sol_data.get("data", [{}])[0].get("price", 0) or 0)
    
    # Scan NEW + MIGRATING stages with API-level buy tx filter (skip dead tokens)
    all_tokens = []
    for stage in ['NEW', 'MIGRATING']:
        # Only fetch tokens with at least 1 buy tx in last hour (active tokens)
        result = _run_cmd(['onchainos', 'memepump', 'tokens', '--chain', 'solana', '--stage', stage, '--min-buy-tx-count', '1'])
        tokens_data = result.get("data", [])
        for t in tokens_data:
            t['_stage'] = stage
        all_tokens.extend(tokens_data)
    
    tokens_data = all_tokens
    
    if not tokens_data:
        print("No tokens found")
        return 0
    
    # P2: 第一遍扫描 - 收集 Twitter handle 重复次数（检测批量发币）
    twitter_handle_map = {}
    for t in tokens_data:
        social = t.get("social", {})
        x_url = social.get("x", "") or ""
        handle = _extract_twitter_handle(x_url)
        if handle:
            twitter_handle_map[handle] = twitter_handle_map.get(handle, 0) + 1
    
    scored_tokens = []
    for t in tokens_data:
        addr = t.get("tokenAddress", "")
        if not addr: continue
        
        price_data = _run_cmd(['onchainos', 'token', 'price-info', '--chain', 'solana', '--address', addr])
        pdata = price_data.get("data", [{}])[0] if price_data.get("data") else {}
        if not pdata: continue
        
        adv_data = _run_cmd(['onchainos', 'token', 'advanced-info', '--chain', 'solana', '--address', addr])
        adv = adv_data.get("data", {})
        
        # Key data points
        mc = float(pdata.get("marketCap", 0) or 0)  # USD - market cap
        pool_usdt = float(pdata.get("liquidity", 0) or 0)  # USDT - pool size (already in USDT!)
        pool_sol = pool_usdt / sol_price if sol_price > 0 else 0  # Convert to SOL equivalent
        vol_usdt = float(pdata.get("volume24H", 0) or 0)  # USDT - 24h volume (already in USDT!)
        price = float(pdata.get("price", 0) or 0)  # price per token
        holders = int(pdata.get("holders", 0) or 0)
        top10 = float(adv.get("top10HoldPercent", 0) or 0) if isinstance(adv, dict) else 0
        sniper = float(adv.get("sniperHoldingPercent", 0) or 0) if isinstance(adv, dict) else 0
        create_time = int(adv.get("createTime", 0) or 0) if isinstance(adv, dict) else 0
        bp = float(t.get("bondingPercent", 0) or 0)
        
        # P0: Dev持仓比例 (最危险的Rug指标)
        dev_holding = float(adv.get("devHoldingPercent", 0) or 0) if isinstance(adv, dict) else 0
        # P0: Anti-Bot标记 (isInternal=true表示有防机器人保护)
        is_internal = adv.get("isInternal", False) if isinstance(adv, dict) else False
        # Dev历史: 发币数量、Rug数量
        dev_launched = int(adv.get("devLaunchedTokenCount", 0) or 0) if isinstance(adv, dict) else 0
        dev_rugs = int(adv.get("devRugPullTokenCount", 0) or 0) if isinstance(adv, dict) else 0
        
        # P1: 买卖笔数比
        market = t.get("market", {})
        buy_tx = int(market.get("buyTxCount1h", 0) or 0)
        sell_tx = int(market.get("sellTxCount1h", 0) or 0)
        
        # P1: 社交媒体存在性
        social = t.get("social", {})
        x_url = social.get("x", "") or ""
        has_x = bool(x_url != "")
        has_tg = bool(social.get("telegram", "") and social.get("telegram") != "")
        has_web = bool(social.get("website", "") and social.get("website") != "")
        has_social = has_x or has_tg or has_web
        
        # P2: Twitter handle + 批量发币检测
        twitter_handle = _extract_twitter_handle(x_url)
        twitter_reuse_count = twitter_handle_map.get(twitter_handle, 0) if twitter_handle else 0
        
        # P2: 价格动量 (5分钟价格变化)
        price_chg5m = float(pdata.get("priceChange5M", 0) or 0)
        
        score = _calc_score(pool_usdt, holders, top10, bp, mc, vol_usdt, dev_holding, dev_rugs, dev_launched, buy_tx, sell_tx, has_social, price_chg5m, twitter_handle, twitter_reuse_count)
        
        scored_tokens.append({
            'symbol': t.get('symbol', '???'),
            'name': t.get('name', '???'),
            'address': addr,
            'score': score,
            'price': price,
            'price_usd': price,
            'mc': mc,
            'pool_usdt': pool_usdt,
            'pool_sol': pool_sol,
            'vol_usdt': vol_usdt,
            'holders': holders,
            'top10': top10,
            'sniper': sniper,
            'create_time': create_time,
            'bonding': bp,
            'chain_index': t.get('chainIndex', ''),
            'dev_holding': dev_holding,
            'is_internal': is_internal,
            'dev_launched': dev_launched,
            'dev_rugs': dev_rugs,
            'buy_tx': buy_tx,
            'sell_tx': sell_tx,
            'has_social': has_social,
            'has_x': has_x,
            'has_tg': has_tg,
            'has_web': has_web,
            'stage': t.get('_stage', 'NEW'),
            'price_chg5m': price_chg5m,
            'twitter_handle': twitter_handle,
            'twitter_reuse_count': twitter_reuse_count,
        })
    
    scored_tokens.sort(key=lambda x: x['score'], reverse=True)
    
    print(f"Scanned {len(scored_tokens)} tokens. Top: {scored_tokens[0]['score'] if scored_tokens else 0}")
    
    high_score = [t for t in scored_tokens if t['score'] >= min_score][:max_per_scan]
    if not high_score:
        return 0
    
    from core.telegram_notifier import load_notifier_from_config
    notifier = load_notifier_from_config("/root/.openclaw/workspace/meme-bot/config.json")
    
    sent_count = 0
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    
    for tok in high_score:
        age_s = _fmt_age(tok['create_time'])
        price_s = _fmt_price(tok['price'])
        mc_s = _fmt_usd(tok['mc'])
        pool_sol_s = _fmt_sol(tok['pool_sol'])
        pool_usd_s = _fmt_usd(tok['pool_usdt'])
        vol_s = _fmt_usd(tok['vol_usdt'])
        risk_label = _get_risk_label(tok['sniper'])
        dev_risk = _get_dev_risk_label(tok['dev_holding'], tok['dev_rugs'], tok.get('dev_launched', 0))
        antibot = _get_antibot_label(tok.get('is_internal', False))
        
        if tok['score'] >= 85: badge = "💎"
        elif tok['score'] >= 75: badge = "✅"
        elif tok['score'] >= 65: badge = "🟡"
        elif tok['score'] >= 50: badge = "🟠"
        else: badge = "🔴"
        
        if tok['score'] >= 85: rec = "💎 顶级金狗，强烈建议关注"
        elif tok['score'] >= 75: rec = "✅ 良好机会，可考虑建仓"
        elif tok['score'] >= 65: rec = "🟡 一般机会，轻仓观察"
        elif tok['score'] >= 50: rec = "🟠 风险较高，仅建议观望"
        else: rec = "🔴 高风险，不建议参与"
        
        # P0额外扣分说明
        penalty_notes = []
        if tok['dev_holding'] >= 10: penalty_notes.append(f"Dev持仓{tok['dev_holding']:.1f}%扣分")
        if tok.get('dev_rugs', 0) >= 1: penalty_notes.append(f"Dev历史Rug{tok['dev_rugs']}次扣分")
        if tok['top10'] >= 70: penalty_notes.append(f"前十{tok['top10']:.1f}%重度老鼠仓")
        if tok.get('twitter_reuse_count', 0) >= 3: penalty_notes.append(f"X批量发币{tok['twitter_reuse_count']}次扣分")
        penalty_str = " | ".join(penalty_notes) if penalty_notes else "无"
        
        # P1: 买卖比显示
        buy_tx = tok.get('buy_tx', 0)
        sell_tx = tok.get('sell_tx', 0)
        if sell_tx > 0:
            tx_ratio = buy_tx / sell_tx
            if tx_ratio >= 10: tx_label = f"🟢 强买 {buy_tx}:{sell_tx} ({tx_ratio:.0f}x)"
            elif tx_ratio >= 5: tx_label = f"🟢 买强 {buy_tx}:{sell_tx} ({tx_ratio:.0f}x)"
            elif tx_ratio >= 2: tx_label = f"🟡 偏买 {buy_tx}:{sell_tx} ({tx_ratio:.1f}x)"
            elif tx_ratio > 1: tx_label = f"🟡 微买 {buy_tx}:{sell_tx}"
            else: tx_label = f"🔴 卖多 {buy_tx}:{sell_tx}"
        elif buy_tx > 0:
            tx_label = f"🟢 纯买 {buy_tx}:0 (吸筹中)"
        else:
            tx_label = "⚪ 无交易"
        
        # P2: 价格动量显示
        chg5m = tok.get('price_chg5m', 0)
        if chg5m >= 100: chg_label = f"🚀 {chg5m:.0f}%"
        elif chg5m >= 50: chg_label = f"📈 {chg5m:.0f}%"
        elif chg5m >= 20: chg_label = f"🟢 {chg5m:.0f}%"
        elif chg5m >= 5: chg_label = f"🟡 {chg5m:.0f}%"
        elif chg5m >= 0: chg_label = f"⚪ {chg5m:.0f}%"
        elif chg5m >= -20: chg_label = f"🟠 {chg5m:.0f}%"
        else: chg_label = f"🔴 {chg5m:.0f}%"
        
        # P1: 社交媒体标签
        social_parts = []
        if tok.get('has_x'): social_parts.append("X")
        if tok.get('has_tg'): social_parts.append("TG")
        if tok.get('has_web'): social_parts.append("Web")
        social_str = " | ".join(social_parts) if social_parts else "❌ 无社交媒体"
        
        # P2: Twitter批量发币警告
        tw_handle = tok.get('twitter_handle', '')
        tw_reuse = tok.get('twitter_reuse_count', 0)
        if tw_reuse >= 5: tw_warning = f"🚨 同一X发币{tw_reuse}次"
        elif tw_reuse >= 3: tw_warning = f"⚠️ 同一X发币{tw_reuse}次"
        elif tw_reuse >= 2: tw_warning = f"⚠️ X重复发布{tw_reuse}次"
        else: tw_warning = None
        tw_display = f"(@{tw_handle})" if tw_handle else ""
        
        # Format: 🏷️ 代币名称 (symbol)
        msg = f"""🏷️ {tok['name']}（{tok['symbol']}）
📛 合约: `{tok['address']}`

━━━━━━ 基本信息 ━━━━━━
🔗 链: SOLANA
📍 阶段: {tok.get('stage', 'NEW')}
⏱️ 发行: {age_s}
💰 价格: {price_s}
💎 市值: {mc_s}
🏦 池子: {pool_usd_s} USDT（{tok['pool_sol']:.1f} SOL）
📊 24h 交易量: {vol_s}
👥 持币人数: {tok['holders']}
📱 社交媒体: {social_str} {tw_display}

━━━━━━ 交易动态 ━━━━━━
💹 1h买卖: {tx_label}
🚀 5分钟涨跌: {chg_label}

━━━━━━ 安全检测 ━━━━━━
🤖 发射模式: {antibot}
🔰 发射历史: Dev共发布{tok['dev_launched']}币 | Rug {tok['dev_rugs']}次
👤 Dev持仓: {tok['dev_holding']:.1f}% | {dev_risk}
🐀 老鼠仓: {tok['sniper']:.1f}% | {risk_label}
📊 前十持仓: {tok['top10']:.1f}%
{f'⚠️ {tw_warning}' if tw_warning else '✅ X账号无批量发币记录'}
⚠️ 扣分项: {penalty_str}

━━━━━━ 信号评分 ━━━━━━
{badge} 评分: {tok['score']}/100
📊 Bonding: {tok['bonding']:.0f}%

━━━━━━ 交易建议 ━━━━━━
{rec}

🕐 {now_str}"""
        
        if notifier:
            _save_signal(tok, tok['score'])
            
            # 检查是否为推送+交易模式，是则添加确认按钮
            reply_markup = None
            try:
                cfg = ConfigManager()
                if cfg.mode == BotMode.SIGNAL_AND_TRADE:
                    # 按钮数据：买入确认、拒绝、忽略
                    addr = tok['address']
                    reply_markup = {
                        "inline_keyboard": [[
                            {"text": "✅ 确认买入", "callback_data": f"CONFIRM_BUY_FROM_SCAN:{addr}"},
                            {"text": "❌ 拒绝", "callback_data": f"REJECT_SCAN:{addr}"},
                            {"text": "🚫 忽略此币", "callback_data": f"IGNORE_SCAN:{addr}"}
                        ]]
                    }
            except Exception:
                pass  # 配置读取失败则不添加按钮
            
            async def send():
                addr = tok['address']
                await notifier.send_message(msg, reply_markup=reply_markup)
            asyncio.run(send())
            print(f"✅ Sent: {tok['symbol']} (score={tok['score']}, dev_holding={tok['dev_holding']:.1f}%, sniper={tok['sniper']:.1f}%, top10={tok['top10']:.1f}%)")
            sent_count += 1
    
    return sent_count

if __name__ == "__main__":
    count = scan_and_send(min_score=50, max_per_scan=3)
    print(f"Done. Sent {count} signals.")
