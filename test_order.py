"""
測試下單環境是否正常

檢查：
1. ClobClient 初始化
2. 錢包餘額 / allowance
3. 下一筆最小限價單（$0.01 買 Yes，不會成交，馬上取消）
"""
import os
from dotenv import load_dotenv

load_dotenv()


def main():
    # 1. 初始化 ClobClient
    print("1. 初始化 ClobClient...")
    from py_clob_client.client import ClobClient

    privKey = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not privKey:
        print("   ❌ POLYMARKET_PRIVATE_KEY not found")
        return

    funder = os.environ.get("POLYMARKET_FUNDER")
    client = ClobClient(
        "https://clob.polymarket.com",
        key=privKey,
        chain_id=137,
        signature_type=2,
        funder=funder,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    from web3 import Account
    addr = Account.from_key(privKey).address
    print(f"   錢包地址：{addr}")
    print("   ✅ ClobClient OK")

    # 2. 查餘額
    print("\n2. 查詢餘額 / allowance...")
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

    bal = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    print(f"   USDC 餘額   : {bal.get('balance', 'N/A')}")
    print(f"   USDC allowance: {bal.get('allowance', 'N/A')}")

    # 3. 測試下單（極低價 $0.01，不會成交）
    print("\n3. 測試下單（$0.01 限價單，會立刻取消）...")
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    from polymarket_data import fetchDailyAboveEvent
    from datetime import date

    event = fetchDailyAboveEvent(date.today())
    if not event or not event["markets"]:
        print("   ⚠️ 找不到今日事件，跳過下單測試")
        return

    tokenId = None
    for m in event["markets"]:
        tid = m.get("clobYesTokenId")
        if tid:
            tokenId = tid
            print(f"   使用關卡：{m.get('question', 'unknown')}")
            break

    if not tokenId:
        print("   ⚠️ 找不到 tokenId，跳過下單測試")
        return

    testSizes = [5.0, 4.0, 3.0, 2.0, 1.0, 0.5]
    for size in testSizes:
        try:
            order = OrderArgs(token_id=tokenId, price=0.01, size=size, side=BUY)
            signed = client.create_order(order)
            resp = client.post_order(signed, OrderType.GTC)
            orderId = resp.get("orderID")
            if orderId:
                client.cancel(orderId)
            print(f"   size={size:>4} → ✅ 成功")
        except Exception as e:
            print(f"   size={size:>4} → ❌ {e}")
            break

    print("\n   測試完成！")


if __name__ == "__main__":
    main()
