"""
One-time script: approve USDC.e for the Polymarket contracts.

Run: venv/bin/python approve.py
"""
import os
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

RPC_URL = os.environ.get("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

SPENDERS = [
    ("CTF Exchange", "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"),
    ("Neg Risk CTF Exchange", "0xC5d563A36AE78145C45a50134d48A1215220f80a"),
    ("Neg Risk Adapter", "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
]

ERC20_ABI = '[{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"}]'

ERC1155_ABI = '[{"constant":false,"inputs":[{"name":"_operator","type":"address"},{"name":"_approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"payable":false,"stateMutability":"nonpayable","type":"function"}]'

MAX_UINT = 2**256 - 1


def main():
    privKey = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not privKey:
        print("POLYMARKET_PRIVATE_KEY not found in .env")
        return

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print("Cannot connect to Polygon RPC")
        return

    account = w3.eth.account.from_key(privKey)
    pubKey = account.address
    print(f"Wallet address: {pubKey}")
    print(f"POL balance: {w3.from_wei(w3.eth.get_balance(pubKey), 'ether'):.4f}\n")

    usdc = w3.eth.contract(address=USDC_ADDRESS, abi=ERC20_ABI)
    ctf = w3.eth.contract(address=CTF_ADDRESS, abi=ERC1155_ABI)

    for name, spender in SPENDERS:
        # 1. Approve USDC.e
        print(f"[1/2] Approve USDC.e → {name}...")
        nonce = w3.eth.get_transaction_count(pubKey)
        txn = usdc.functions.approve(spender, MAX_UINT).build_transaction({
            "chainId": 137,
            "from": pubKey,
            "nonce": nonce,
        })
        signed = w3.eth.account.sign_transaction(txn, private_key=privKey)
        txHash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(txHash, 600)
        print(f"  OK tx: {receipt.transactionHash.hex()}")

        # 2. Approve Conditional Token (ERC1155)
        print(f"[2/2] Approve CTF → {name}...")
        nonce = w3.eth.get_transaction_count(pubKey)
        txn = ctf.functions.setApprovalForAll(spender, True).build_transaction({
            "chainId": 137,
            "from": pubKey,
            "nonce": nonce,
        })
        signed = w3.eth.account.sign_transaction(txn, private_key=privKey)
        txHash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(txHash, 600)
        print(f"  OK tx: {receipt.transactionHash.hex()}")

    print("\nAll approvals done! You can place orders now.")


if __name__ == "__main__":
    main()
