"""
Trading environment check: initialize ClobClient (auto-applies proxy + V2 auth)
and query the USDC balance / allowance.

Run: venv/bin/python test_order.py
"""
from lp_maker import initClobClient, setupLogging
from py_clob_client_v2 import BalanceAllowanceParams, AssetType


def main():
    setupLogging()

    print("Initializing ClobClient (applying proxy + V2 auth)...")
    client = initClobClient()
    print("ClobClient OK")

    print("\nQuerying USDC balance / allowance...")
    bal = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    print(f"  response: {bal}")


if __name__ == "__main__":
    main()
