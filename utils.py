import os
import base64
import random
import aiohttp
import asyncio
import hashlib
import binascii
import requests

from web3 import Web3
from loguru import logger
from Crypto.Cipher import AES
from fake_useragent import UserAgent
from Crypto.Util.Padding import unpad
from Crypto.Protocol.KDF import PBKDF2


from networks import Etherium
from contracts import Token
from config import script_name, consumer_tg_id, tg_bot_token


class Wallet:
    def __init__(
            self,
            private_key: str,
            done_wallets: list,
            cluster: int | None = None,
            back_wallet: str | None = None
    ):

        self.private_key = private_key
        self.address = Etherium.eth.account.from_key(self.private_key).address
        self.valid = True if self.address not in done_wallets else False
        self.back_wallet = Web3.to_checksum_address(back_wallet) if back_wallet else None
        self.cluster = cluster if cluster else 0

        self.position = {
            "pool": "",
            "token1": "",
            "token2": "",
            "fee_tier": 0.00,
            "pos_id": 0,
            "tick_lower": 0,
            "tick_upper": 0,
            "liquidity": 0,
            "high_price": 0.00,
            "low_price": 0.00
        }


def load_lines(filename: str) -> list:
    with open(filename) as f:
        return [row.strip() for row in f if row and not row.startswith('#')]


def is_base64(s):
    if not len(s):
        return False

    try:
        if len(s) == 64:
            Web3().eth.account.from_key(s)
            return False

    except Exception:
        pass

    try:
        decoded = base64.b64decode(s)
        reencoded = base64.b64encode(decoded)
        return reencoded == s.encode()

    except Exception:
        return False


def get_cipher(password):
    salt = hashlib.sha256(password.encode('utf-8')).digest()
    key = PBKDF2(password.encode('utf-8'), salt, dkLen=32, count=1)
    return AES.new(key, AES.MODE_ECB)


def decrypt_private_key(encrypted_base64_pk, password):
    cipher = get_cipher(password)
    encrypted_pk = base64.b64decode(encrypted_base64_pk)
    decrypted_bytes = unpad(cipher.decrypt(encrypted_pk), 16)
    decrypted_hex = binascii.hexlify(decrypted_bytes).decode()

    if len(decrypted_hex) in (66, 42):
        decrypted_hex = decrypted_hex[2:]

    return '0x' + decrypted_hex


def load_and_decrypt_wallets(filename, password='', shuffle=False):
    lines = load_lines(filename)
    done_wallets = load_lines("files/done_wallets.txt")
    wallets = []

    for line in lines:
        if password and is_base64(line):
            _pkey = decrypt_private_key(line, password)

        else:
            _pkey = line

        wallets.append(
            Wallet(
                private_key=_pkey,
                done_wallets=done_wallets
            )
        )

    wallets = [wallet for wallet in wallets if wallet.valid]

    if shuffle:
        random.shuffle(wallets)

    return wallets


def get_proxies(path: str) -> list:
    with open(path, "r") as f:
        return [el.strip() for el in f.readlines()]


async def send_status(message_text: str, wallet: Wallet | None = None) -> None:
    if wallet:
        message_text = f"{wallet.address}\n{message_text}"

    requests.post(
        f"https://api.telegram.org/{tg_bot_token}/sendMessage?chat_id={consumer_tg_id}&text={script_name}:\n{message_text}"
    )


async def finalize(wallet: Wallet, notify: bool | None = True) -> None:
    with open("files/done_wallets.txt", "a") as f:
        f.write(f"{wallet.address}\n")

    if notify:
        await send_status(f"DONE:\n{wallet.address}")


async def get_price(symbol: str) -> float:
    if symbol in ["USDT", "USDC"]:
        return 1.0

    url = f"https://api.binance.com/api/v3/depth?limit=1&symbol={symbol}USDT"

    ua = UserAgent()
    headers = {"User-Agent": ua.random}

    for _ in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                session.headers.update(headers)

                async with session.get(url=url, timeout=30) as res:
                    if res.status == 200:
                        data = await res.json()
                        return float(data['asks'][0][0])

                    else:
                        logger.warning(f"Request finished with code {res.status}: {await res.text()}")
                        await asyncio.sleep(random.randint(5, 10))
                        continue

        except Exception as e:
            logger.warning(f"Something went wrong while getting token price via {url}")
            logger.warning(f"{type(e).__name__}: {e}")
            await asyncio.sleep(random.randint(5, 10))

    logger.error(f"Can't get price for {symbol}")
    raise Exception(f"Can't get price for {symbol}")


async def approved(token: Token, spender: str, amount: int, wallet: Wallet) -> bool:
    allowance = await token.contract.functions.allowance(
        wallet.address,
        spender
    ).call()

    if allowance >= amount:
        logger.success(f"Already approved {round(amount / (10 ** token.decimals), int(token.decimals / 3))} {token.symbol}")
        return True

    else:
        logger.info(f"Need to approve {round(amount / (10 ** token.decimals), int(token.decimals / 3))} {token.symbol}")
        return False


async def approve(token: Token, spender: str, amount: int, wallet: Wallet) -> None:
    tx = await token.contract.functions.approve(
        spender,
        amount
    ).build_transaction({
        "chainId": token.chain.chain_id,
        "from": wallet.address,
        "nonce": await token.chain.provider.eth.get_transaction_count(wallet.address)
    })

    signed_txn = token.chain.provider.eth.account.sign_transaction(tx, private_key=wallet.private_key)
    send_txn = await token.chain.provider.eth.send_raw_transaction(signed_txn.rawTransaction)
    tx_receipt = await token.chain.provider.eth.wait_for_transaction_receipt(send_txn)

    if tx_receipt['status'] == 1:
        logger.success(f"{wallet.address}: Approved {round(amount / (10 ** token.decimals), int(token.decimals / 3))} {token.chain.name} {token.symbol} for {spender} | {tx_receipt['transactionHash'].hex()}")
        await asyncio.sleep(random.randint(15, 45))

    else:
        logger.error(f"{wallet.address}: Not approved [{tx}: {tx_receipt}]")
        raise Exception(f"{wallet.address}: Not approved [{tx}: {tx_receipt}]")
