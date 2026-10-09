import asyncio
import aiohttp
import time
from datetime import datetime
import sys
import itertools
import importlib

# =====================================================================
# --- Dynamic Configuration Loader ---
# =====================================================================
config_name = sys.argv[1] if len(sys.argv) > 1 else "config"
if config_name.endswith(".py"):
    config_name = config_name[:-3]

try:
    config = importlib.import_module(config_name)
    print(f"⚙️ Loaded configuration: {config_name}.py")
except ImportError:
    print(f"\n⚠️ Error: '{config_name}.py' not found!")
    sys.exit(1)

# Wallet & Bot Configuration
TARGET_WALLETS = config.TARGET_WALLETS
POLL_INTERVAL = getattr(config, 'POLL_INTERVAL', 2)
TELEGRAM_BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = config.TELEGRAM_CHAT_ID

# Official Phoenix Program IDs on Solana Mainnet
PHOENIX_PROGRAMS = {
    "PhoeNiX2EyyJBKw5EaWZbg8hkfz3DcjMBNfmsyJR8qQ": "Phoenix Spot 🦅",
    "EtrnLzgbS7nMMy5fbD42kXiUzGg8XQzJ972Xtk1cjWih": "Phoenix Eternal (Perps) ⚡"
}

# RPC Setup (Helius + Auto-failover Backups)
HELIUS_API_KEYS = getattr(config, 'HELIUS_API_KEYS', [])
if not HELIUS_API_KEYS and hasattr(config, 'HELIUS_API_KEY') and config.HELIUS_API_KEY:
    HELIUS_API_KEYS = [config.HELIUS_API_KEY]

helius_urls = [f"https://mainnet.helius-rpc.com/?api-key={k}" for k in HELIUS_API_KEYS if k and "your-" not in str(k)]

backup_rpcs = getattr(config, 'RPC_URLS', [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
    "https://rpc.ankr.com/solana"
])

RPC_URLS = helius_urls + [url for url in backup_rpcs if url not in helius_urls]
helius_cycle = itertools.cycle(RPC_URLS) if RPC_URLS else None

last_429_warn_time = 0
AI_PROVIDERS = getattr(config, 'AI_PROVIDERS', [])


def get_next_rpc_url():
    if helius_cycle:
        return next(helius_cycle)
    return RPC_URLS[0]


async def send_telegram_alert(session: aiohttp.ClientSession, message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        async with session.post(url, json=payload, timeout=10) as resp:
            pass
    except Exception:
        pass


async def fetch_rpc(session: aiohttp.ClientSession, method: str, params: list):
    global last_429_warn_time
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    attempts = len(RPC_URLS) if RPC_URLS else 1

    for _ in range(attempts):
        rpc_url = get_next_rpc_url()
        try:
            async with session.post(rpc_url, json=payload, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "result" in data:
                        return data["result"]
                elif resp.status == 429:
                    now = time.time()
                    if now - last_429_warn_time > 15:
                        last_429_warn_time = now
                        print("⏳ Helius Rate Limit (429) hit. Automatically switching to backup RPC...")
                    continue
        except Exception:
            continue
    return None


# =====================================================================
# --- Direct Phoenix Perps JSON API Integration ---
# =====================================================================
async def get_phoenix_portfolio_snapshot(session: aiohttp.ClientSession, wallet_addr: str) -> str:
    """Fetches Phoenix Eternal portfolio & collateral data directly from official JSON APIs in <100ms."""
    base_url = f"https://perp-api.phoenix.trade/v1/users/{wallet_addr}"
    totals_url = f"{base_url}/collateral-totals"
    history_url = f"{base_url}/collateral-history-v2?limit=5"

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    summary_parts = []

    try:
        # 1. Fetch Collateral Totals
        async with session.get(totals_url, headers=headers, timeout=5) as resp_totals:
            if resp_totals.status == 200:
                totals_data = await resp_totals.json()
                total_dep = totals_data.get("totalDeposited", 0)
                total_with = totals_data.get("totalWithdrawn", 0)
                assets = totals_data.get("assets", [])

                asset_str_list = []
                for a in assets:
                    sym = a.get("symbol", "Asset")
                    dep_val = a.get("deposited", {}).get("value", 0)
                    asset_str_list.append(f"{sym}: ${dep_val:,.2f}")

                asset_str = ", ".join(asset_str_list) if asset_str_list else "None"
                summary_parts.append(
                    f"📊 **موجودی کل کولترال:** Deposited: ${total_dep:,.2f} | Withdrawn: ${total_with:,.2f} | Assets: [{asset_str}]"
                )

        # 2. Fetch Recent Collateral History
        async with session.get(history_url, headers=headers, timeout=5) as resp_hist:
            if resp_hist.status == 200:
                hist_data = await resp_hist.json()
                events = hist_data.get("data", [])
                if events:
                    last_evt = events[0]
                    category = last_evt.get("category", "N/A")
                    symbol = last_evt.get("symbol", "N/A")
                    bal_after = float(last_evt.get("balanceAfter", 0)) / (10 ** 6)
                    ts = last_evt.get("timestamp", "N/A")
                    summary_parts.append(
                        f"⏱ **آخرین رویداد کولترال:** Category: {category} | Asset: {symbol} | Balance After: ${bal_after:,.2f} | Time: {ts}"
                    )

    except Exception as e:
        print(f"⚠️ Direct Phoenix API Error: {e}")
        return "اطلاعات زنده API در دسترس نیست."

    return "\n".join(summary_parts) if summary_parts else "داده‌ای برای این ولت یافت نشد."


def extract_balance_changes(tx_info: dict) -> str:
    """Extracts human-readable token balance changes from transaction metadata."""
    if not tx_info or "meta" not in tx_info:
        return "تغییرات بالانس ثبت نشده است."

    meta = tx_info["meta"]
    pre_balances = meta.get("preTokenBalances", [])
    post_balances = meta.get("postTokenBalances", [])

    changes = []
    pre_map = {b.get("accountIndex"): b for b in pre_balances if b.get("accountIndex") is not None}

    for post in post_balances:
        idx = post.get("accountIndex")
        mint = post.get("mint", "Unknown")
        post_amount = float(post.get("uiTokenAmount", {}).get("uiAmount") or 0)

        pre = pre_map.get(idx, {})
        pre_amount = float(pre.get("uiTokenAmount", {}).get("uiAmount") or 0) if pre else 0.0

        diff = post_amount - pre_amount
        if abs(diff) > 0.000001:
            symbol = mint[:4] + "..." + mint[-4:] if len(mint) > 10 else mint
            sign = "+" if diff > 0 else ""
            changes.append(f"• توکن ({symbol}): {sign}{diff:,.4f} (موجودی جدید: {post_amount:,.4f})")

    return "\n".join(changes) if changes else "تغییر مستقیمی در کیف‌پول اصلی صورت نگرفته (مدیریت پوزیشن/مارجین داخل صرافی)."


# =====================================================================
# --- AI Providers Handlers ---
# =====================================================================
async def call_gemini(session: aiohttp.ClientSession, key: str, model: str, prompt: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model or 'gemini-2.0-flash'}:generateContent?key={key}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        async with session.post(url, json=payload, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data['candidates'][0]['content']['parts'][0]['text'].strip()
    except Exception as e:
        print(f"⚠️ Gemini Exception: {e}")
    return ""


async def call_openrouter(session: aiohttp.ClientSession, key: str, model: str, prompt: str) -> str:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": model or "~openai/gpt-sol-latest",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 250,
        "temperature": 0.2
    }
    try:
        async with session.post(url, headers=headers, json=payload, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data['choices'][0]['message']['content'].strip()
            else:
                err_text = await resp.text()
                print(f"⚠️ OpenRouter Error [{resp.status}]: {err_text[:120]}")
    except Exception as e:
        print(f"⚠️ OpenRouter Exception: {e}")
    return ""


async def call_deepseek(session: aiohttp.ClientSession, key: str, model: str, prompt: str) -> str:
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": model or "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 250
    }
    try:
        async with session.post(url, headers=headers, json=payload, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f"⚠️ DeepSeek Exception: {e}")
    return ""


async def get_ai_tx_analysis(session: aiohttp.ClientSession, logs: list, action_type: str, market_type: str, balance_summary: str, portfolio_snapshot: str) -> str:
    """Combines live API data + on-chain logs for high-accuracy AI analysis."""
    if not AI_PROVIDERS:
        return ""

    logs_text = " ".join(logs[:15])

    prompt = (
        f"نقش تو: تحلیل‌گر ارشد معاملات فیوچرز (Perpetuals) صرافی Phoenix روی سولانا.\n\n"
        f"📊 **اطلاعات استخراج‌شده از API رسمی صرافی (Phoenix Eternal):**\n"
        f"{portfolio_snapshot}\n\n"
        f"🌐 **مشخصات دستور شبکه:**\n"
        f"- بخش: {market_type}\n"
        f"- دستور: {action_type}\n"
        f"- تغییرات بالانس ولت: {balance_summary}\n"
        f"- لاگ‌های فنی: {logs_text}\n\n"
        f"🎯 **وظیفه:**\n"
        f"با ترکیب داده‌های زنده API صرافی و دستور جدید، در ۳ خط کوتاه و با ذکر اعداد تحلیل کن:\n"
        f"۱. چه ارزی معامله یا مدیریت شده و حجم پوزیشن/مارجین چقدر است؟\n"
        f"۲. وضعیت کولترال و موجودی حساب معامله‌گر چیست؟\n"
        f"۳. استراتژی این دستور جدید (مثلاً لیمیت اردر، تسویه مارجین، واریز/برداشت) دقیقاً چیست؟\n\n"
        f"⚠️ **قواعد:** بدون سلام، مقدمه یا عبارت‌های کلیشه. فقط تحلیل عددی و کاربردی تحویل بده."
    )

    for item in AI_PROVIDERS:
        provider = item.get("provider", "").lower()
        key = item.get("api_key", "")
        model = item.get("model", "")

        if not key or "YOUR_" in key:
            continue

        try:
            result = ""
            if provider == "openrouter":
                result = await call_openrouter(session, key, model, prompt)
            elif provider == "gemini":
                result = await call_gemini(session, key, model, prompt)
            elif provider == "deepseek":
                result = await call_deepseek(session, key, model, prompt)

            if result:
                result = result.replace("<", "&lt;").replace(">", "&gt;")
                return f"\n🧠 <b>تحلیل هوشمند معامله (داده‌های زنده API):</b>\n<i>{result}</i>\n"
        except Exception as e:
            print(f"⚠️ AI Exec Error [{provider}]: {e}")
            continue

    return ""


def get_phoenix_type(tx_info: dict) -> str:
    if not tx_info:
        return None
    try:
        message = tx_info.get("transaction", {}).get("message", {})
        account_keys = message.get("accountKeys", [])
        for acc in account_keys:
            pubkey = acc.get("pubkey") if isinstance(acc, dict) else acc
            if pubkey in PHOENIX_PROGRAMS:
                return PHOENIX_PROGRAMS[pubkey]
    except Exception:
        pass
    return None


def quick_detect_action(logs: list) -> str:
    if not logs:
        return "⚡ معامله / مدیریت سفارش"

    logs_str = " ".join(logs).lower()
    if "placelimit" in logs_str or "place_limit" in logs_str:
        return "📥 ثبت سفارش لیمیت (Limit Order)"
    elif "placemarket" in logs_str or "place_market" in logs_str:
        return "⚡ معامله مارکت (Market Order)"
    elif "cancel" in logs_str:
        return "❌ لغو سفارش (Cancel Order)"

    return "⚡ معامله / مدیریت سفارش"


async def monitor_wallet(session: aiohttp.ClientSession, wallet_addr: str, wallet_name: str, last_signatures: dict):
    sigs = await fetch_rpc(session, "getSignaturesForAddress", [wallet_addr, {"limit": 20}])
    if not sigs:
        return

    last_sig = last_signatures.get(wallet_addr)
    if last_sig is None:
        last_signatures[wallet_addr] = sigs[0]["signature"]
        print(f"✅ Monitoring active for [{wallet_name}]")
        return

    new_sigs = []
    for sig_info in sigs:
        sig = sig_info["signature"]
        if sig == last_sig:
            break
        new_sigs.append(sig_info)

    if new_sigs:
        last_signatures[wallet_addr] = new_sigs[0]["signature"]

        for sig_info in reversed(new_sigs):
            sig = sig_info["signature"]
            err = sig_info.get("err")
            block_time = sig_info.get("blockTime")

            tx_info = await fetch_rpc(session, "getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])

            phoenix_market_type = get_phoenix_type(tx_info)
            if not phoenix_market_type:
                continue

            time_str = datetime.fromtimestamp(block_time).strftime('%Y-%m-%d %H:%M:%S') if block_time else "Unknown"
            status = "❌ Failed" if err else "✅ Success"

            raw_logs = tx_info.get("meta", {}).get("logMessages", []) if tx_info else []
            action_type = quick_detect_action(raw_logs)

            # 1. Extract balance changes
            balance_summary = extract_balance_changes(tx_info)

            # 2. Fetch live portfolio & collateral data directly from Phoenix JSON API (<100ms)
            portfolio_snapshot = await get_phoenix_portfolio_snapshot(session, wallet_addr)

            # 3. AI Analysis with enriched API + on-chain context
            ai_analysis = await get_ai_tx_analysis(session, raw_logs, action_type, phoenix_market_type, balance_summary, portfolio_snapshot)
            phoenix_portfolio_url = f"https://www.phoenix.trade/portfolio/value/all?ghost={wallet_addr}"

            alert_text = (
                f"🦅 <b>تراکنش جدید Phoenix ثبت شد!</b>\n\n"
                f"🏷 <b>نام ولت:</b> {wallet_name}\n"
                f"🏢 <b>بخش:</b> {phoenix_market_type}\n"
                f"👤 <b>آدرس:</b> <code>{wallet_addr[:6]}...{wallet_addr[-4:]}</code>\n"
                f"📌 <b>نوع دستور:</b> {action_type}\n"
                f"📊 <b>وضعیت:</b> {status}\n"
                f"{ai_analysis}"
                f"⏰ <b>زمان:</b> {time_str}\n\n"
                f"💡 <i>جهت مشاهده جزئیات پوزیشن و پورتفولیو:</i>\n"
                f"🔗 <a href='https://solscan.io/tx/{sig}'>مشاهده تراکنش در Solscan</a>\n"
                f"🦅 <a href='{phoenix_portfolio_url}'>مشاهده پورتفولیو زنده در Phoenix</a>"
            )

            asyncio.create_task(send_telegram_alert(session, alert_text))
            print(f"⚡ Alert sent [{phoenix_market_type}] for {wallet_name}: {sig[:8]}")


async def main():
    print(f"🚀 Phoenix Tracker Active using [{config_name}.py] (Direct API Mode)...")
    last_signatures = {}

    async with aiohttp.ClientSession() as session:
        await send_telegram_alert(session, f"🚀 <b>Phoenix Tracker Active ({config_name}.py) with High-Speed Direct API.</b>")

        while True:
            start_time = time.time()

            tasks = [
                monitor_wallet(session, addr, name, last_signatures)
                for addr, name in TARGET_WALLETS.items()
            ]
            await asyncio.gather(*tasks)

            elapsed = time.time() - start_time
            sleep_time = max(0.1, POLL_INTERVAL - elapsed)
            await asyncio.sleep(sleep_time)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot Stopped.")
