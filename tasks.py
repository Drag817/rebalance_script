import time
import json
import base64
import random
import aiohttp
import asyncio

from web3 import Web3
from eth_abi import abi
from loguru import logger
from fake_useragent import UserAgent
from aiohttp_socks import ProxyConnector

from config import SUSHI_SWAP_ABI, position_safe_limit
from utils import Wallet, get_proxies, get_price, approved, approve
from contracts import Contract, Token, Arbitrum_USDC, Arbitrum_ETH
from networks import Network


async def create_position(wallet: Wallet, chain: Network, eth_amount: float) -> None:
    for _ in range(len(chain.urls)):
        url = f"https://interface.gateway.uniswap.org/v2/quote"
        ua = UserAgent()
        proxies = get_proxies("files/proxy.txt")

        params = {
            "tokenInChainId": chain.chain_id,
            "tokenIn": "ETH",
            "tokenOutChainId": chain.chain_id,
            "tokenOut": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            "amount": "10000000000",
            "sendPortionEnabled": False,
            "type": "EXACT_OUTPUT",
            "intent": "pricing",
            "configs": [{
                "enableUniversalRouter": True,
                "protocols": ["V2", "V3", "MIXED"],
                "routingType": "CLASSIC",
                "enableFeeOnTransferFeeFetching": True
            }],
            "useUniswapX": False,
            "slippageTolerance": "0.5"
        }

        headers = {
            'accept': '*/*',
            'origin': 'https://app.uniswap.org',
            'user-agent': ua.random
        }

        proxy = random.choice(proxies)
        connector = ProxyConnector.from_url(f'socks5://{proxy}')

        async with aiohttp.ClientSession(connector=connector) as session:
            session.headers.update(headers)

            for _ in range(3):
                async with session.post(url=url, data=json.dumps(params), timeout=30) as res:
                    if res.status == 200:
                        response = await res.json()
                        current_tick = 10 * round(int(response["quote"]["route"][0][0]["tickCurrent"]) / 10)
                        break

                    else:
                        logger.warning(f"({_ + 1}|3) [{res.status}] {await res.text()}")
                        await asyncio.sleep(random.randint(1, 5))

        tick_lower = current_tick - 150
        tick_upper = current_tick + 150
        token_one_amount = int(eth_amount * (10 ** 18))
        token_two_amount = int(((eth_amount * await get_price("ETH")) * (10 ** 6)) * 0.995)
        token_one_min = int(token_one_amount * random.uniform(0.65, 0.75))
        token_two_min = int(token_two_amount * random.uniform(0.65, 0.75))
        deadline = int(time.time() + 60 * 60 * 12)
        value = token_one_amount

        data = abi.encode(
            ["address", "address", "uint24", "int24", "int24", "uint256", "uint256", "uint256", "uint256", "address", "uint256"],
            [
                "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", # WETH
                "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", # USDC
                500, # fee
                tick_lower,
                tick_upper,
                value, # value
                token_two_amount, # USDC amount
                token_one_min, # value - %
                token_two_min, # USDC - %
                wallet.address,
                deadline # time
            ]
        )

        result = abi.encode(
            ["bytes[]"],
            [(
                Web3.to_bytes(hexstr=f"88316456{data.hex()}"),
                Web3.to_bytes(hexstr=f"12210e8a")
            )]
        )

        if not await approved(Arbitrum_USDC, "0xC36442b4a4522E871399CD717aBDD847Ab11FE88", token_two_amount, wallet):
            await approve(Arbitrum_USDC, "0xC36442b4a4522E871399CD717aBDD847Ab11FE88", token_two_amount, wallet)

        fee = await chain.provider.eth.get_block("latest")
        base_fee = int(fee['baseFeePerGas'] * 1.01)
        priority_fee = await chain.provider.eth.max_priority_fee

        if priority_fee > base_fee:
            base_fee = priority_fee

        tx = {
            "to": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
            "data": f"0xac9650d8{result.hex()}",
            "chainId": chain.chain_id,
            "value": value,
            "nonce": await chain.provider.eth.get_transaction_count(wallet.address),
            "from": wallet.address,
            "maxFeePerGas": base_fee,
            "maxPriorityFeePerGas": priority_fee,
            "gas": 0
        }

        gas_limit = await chain.provider.eth.estimate_gas(tx)
        tx.update({'gas': gas_limit})

        signed_txn = chain.provider.eth.account.sign_transaction(tx, private_key=wallet.private_key)
        send_txn = await chain.provider.eth.send_raw_transaction(signed_txn.rawTransaction)
        tx_receipt = await chain.provider.eth.wait_for_transaction_receipt(send_txn)

        if tx_receipt['status'] == 1:
            logger.success(f"{wallet.address}: Position created | {tx_receipt['transactionHash'].hex()}")

            # with open("files/result.txt", "a") as f:
            #     f.write(f"{wallet.address}: {round(amount / (10 ** token_in.decimals), 2)} {token_in.symbol} -> {round((balance - min_amount_out) / (10 ** token_out.decimals), int(token_out.decimals / 3))} {token_out.symbol} [{token_in.chain.name}] | {tx_receipt['transactionHash'].hex()}\n")

            await asyncio.sleep(random.randint(5, 10))
            return

        else:
            logger.error(f"{wallet.address}: Position not created  [{tx}: {tx_receipt}]")
            raise Exception(f"{wallet.address}: Position not created [{tx}: {tx_receipt}]")


async def balance_enaugh(wallet: Wallet, position_amount: float, token: Token) -> bool:
    ETH_price = await get_price("ETH")
    USDC_needed = (position_amount / 2) * ETH_price
    USDC_balance = await token.contract.functions.balanceOf(wallet.address).call()

    if USDC_balance < USDC_needed * (10 ** token.decimals):
        logger.warning(f"{wallet.address}: {token.symbol} not enough, needs {USDC_needed} {token.symbol}, try to swap ...")
        return False

    else:
        logger.success(f"{wallet.address}: {token.symbol} is enough: {USDC_balance / (10 ** token.decimals)}, needs {USDC_needed}")
        return True


async def in_position(wallet: Wallet, pool_contract: Contract) -> bool:
    positions_count = await pool_contract.contract.functions.balanceOf(wallet.address).call()

    if positions_count == 0:
        logger.warning(f"{wallet.address}: There is no positions, creating ...")
        return False

    position_id = await pool_contract.contract.functions.tokenOfOwnerByIndex(
        wallet.address,
        positions_count - 1
    ).call()

    tick_data = await pool_contract.contract.functions.positions(position_id).call()

    if tick_data[7] == 0:
        logger.warning(f"{wallet.address}: There is no active positions, creating ...")
        return False

    wallet.position["pos_id"] = position_id
    wallet.position["tick_lower"] = tick_data[5]
    wallet.position["tick_upper"] = tick_data[6]
    wallet.position["liquidity"] = tick_data[7]

    positions_raw_data = await pool_contract.contract.functions.tokenURI(position_id).call()
    position_data = json.loads(base64.b64decode(positions_raw_data.split(",")[1]))

    wallet.position["fee_tier"] = int(float(position_data["name"].split(" - ")[1][:-1]) * 10000)
    wallet.position["low_price"] = float(position_data["name"].split(" - ")[-1].split("<>")[0])
    wallet.position["high_price"] = float(position_data["name"].split(" - ")[-1].split("<>")[1])

    # TODO: fix hardcode
    wallet.position["pool"] = position_data["description"].split("Pool Address: ")[1].split("\n")[0]
    wallet.position["token1"] = position_data["description"].split("WETH Address: ")[1].split("\n")[0]
    wallet.position["token2"] = position_data["description"].split("USDC Address: ")[1].split("\n")[0]

    logger.success(f"{wallet.address}: Found position - {wallet.position}")
    return True


async def sushi_swap(wallet: Wallet, token_in: Token, token_out: Token, amount: float | None = None) -> None:
    if amount:
        if token_in.address == "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE":
            amount = int(amount * (10 ** 18))

        else:
            amount = int(amount * (10 ** token_in.decimals))

    else:
        if token_in.address == "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE":
            amount = await token_out.chain.provider.eth.get_balance(wallet.address)
            amount = int(amount * 0.99) # TODO: more detailed calc

        else:
            amount = await token_in.contract.functions.balanceOf(wallet.address).call()

    if amount == 0:
        logger.error(f"There is no {token_in.symbol} on {wallet.address}!")
        raise Exception(f"There is no {token_in.symbol} on {wallet.address}!")

    logger.info(f"Try swap {round(amount / (10 ** token_in.decimals), int(token_in.decimals / 3))} {token_in.symbol} to {token_out.symbol} in {token_in.chain.name}: {wallet.address} ...")

    url = f"https://api.sushi.com/swap/v4/{token_in.chain.chain_id}"
    # url = f"https://api.sushi.com/swap/v5/{token_in.chain.chain_id}"
    proxies = get_proxies("files/proxy.txt")

    ua = UserAgent()
    headers = {"User-Agent": ua.random}

    # if token_out.address != "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE":
    #     last_out_balance = await token_out.contract.functions.balanceOf(wallet.address).call()
    # else:
    #     last_out_balance = await token_out.chain.provider.eth.get_balance(wallet.address)

    params = {
        'tokenIn': f'{token_in.address}',
        'tokenOut': f'{token_out.address}',
        'amount': f'{amount}',
        'maxPriceImpact': '0.005',
        'to': f'{wallet.address}',
        # 'preferSushi': 'true',
        'includeTransaction': 'true',
        'includeRoute': 'true'
    }

    for _ in range(3):
        proxy = random.choice(proxies)
        connector = ProxyConnector.from_url(f'socks5://{proxy}')

        async with aiohttp.ClientSession(connector=connector) as session:
            session.headers.update(headers)

            async with session.get(url=url, params=params, timeout=30) as res:
                if res.status == 200:
                    data = await res.json()

                    if data['status'] == 'Success':
                        break

                    elif data['status'] == 'NoWay':
                        logger.warning(f"{data}")
                        await asyncio.sleep(random.randint(120, 180))

                    else:
                        logger.warning(f"{data}")
                        await asyncio.sleep(_ + 1)

                else:
                    logger.warning(f"Request finished with code {res.status}: {await res.text()}")
                    logger.debug(f"{url}\n{params}")
                    await asyncio.sleep(random.randint(1, 5))
                    continue

    amount_out = int((int(data['assumedAmountOut']) - int(int(data['assumedAmountOut']) * data['priceImpact'])) * 0.995)

    SushiSwap = Contract(
        chain=token_in.chain,
        address=data['routeProcessorAddr'],
        abi=SUSHI_SWAP_ABI
    )

    if token_in.address != "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE":
        if not await approved(token_in, SushiSwap.address, amount, wallet):
            await approve(token_in, SushiSwap.address, amount, wallet)

    if "routeProcessorArgs" not in data.keys():
        fee = await token_in.chain.provider.eth.get_block("latest")
        base_fee = int(fee['baseFeePerGas'] * 1.01)
        priority_fee = await token_in.chain.provider.eth.max_priority_fee

        if priority_fee > base_fee:
            base_fee = priority_fee

        logger.info(data["tx"]["data"])

        tx = {
            "value": amount if token_in.address == "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE" else 0,
            "data": data["tx"]["data"],
            "chainId": token_in.chain.chain_id,
            "from": wallet.address,
            "to": data["tx"]["to"],
            "nonce": await token_in.chain.provider.eth.get_transaction_count(wallet.address),
            'maxFeePerGas': base_fee,
            'maxPriorityFeePerGas': priority_fee,
            'gas': 0
        }

        gas_limit = await token_in.chain.provider.eth.estimate_gas(tx)
        tx.update({'gas': gas_limit})

    else:
        tx = await SushiSwap.contract.functions.processRoute(
            token_in.address,
            amount,
            token_out.address,
            amount_out,
            wallet.address,
            data['routeProcessorArgs']['routeCode']
        ).build_transaction({
            "value": amount if token_in.address == "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE" else 0,
            "chainId": SushiSwap.chain.chain_id,
            "from": wallet.address,
            "nonce": await SushiSwap.chain.provider.eth.get_transaction_count(wallet.address)
        })

    signed_txn = SushiSwap.chain.provider.eth.account.sign_transaction(tx, private_key=wallet.private_key)
    send_txn = await SushiSwap.chain.provider.eth.send_raw_transaction(signed_txn.rawTransaction)
    tx_receipt = await SushiSwap.chain.provider.eth.wait_for_transaction_receipt(send_txn)

    if tx_receipt['status'] == 1:
        logger.success(f"Swapped {round(amount / (10 ** token_in.decimals), int(token_in.decimals / 3))} {token_in.symbol} in {round(amount_out / (10 ** token_out.decimals), int(token_out.decimals / 3))} {token_out.symbol} | {tx_receipt['transactionHash'].hex()}")

        # if token_out.address != "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE":
        #     balance = await token_out.contract.functions.balanceOf(wallet.address).call()
        # else:
        #     balance = await token_out.chain.provider.eth.get_balance(wallet.address)

        # with open("files/result.txt", "a") as f:
        #     f.write(f"{wallet.address}: {round(amount / (10 ** token_in.decimals), 2)} {token_in.symbol} -> {round((balance - last_out_balance) / (10 ** token_out.decimals), int(token_out.decimals / 3))} {token_out.symbol} [{token_in.chain.name}] | {tx_receipt['transactionHash'].hex()}\n")

        await asyncio.sleep(random.randint(5, 10))
        return

    else:
        logger.error(f"Not swapped!")
        raise Exception("Not swapped!")


async def remove_liquidity(wallet: Wallet, chain: Network) -> None:
    decrease = abi.encode(
        ["uint256", "uint128", "uint256", "uint256", "uint256"],
        [
            wallet.position["pos_id"],
            wallet.position["liquidity"],
            0,
            0,
            int(time.time() + 60 * 60 * 12)
        ]
    )

    collect = abi.encode(
        ["uint256", "address", "uint128", "uint128"],
        [
            wallet.position["pos_id"],
            "0x0000000000000000000000000000000000000000",
            340282366920938463463374607431768211455,
            340282366920938463463374607431768211455
        ]
    )

    unwrap = abi.encode(
        ["uint256", "address"],
        [
            0,
            wallet.address
        ]
    )

    sweep = abi.encode(
        ["address", "uint256", "address"],
        [
            "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            0,
            wallet.address
        ]
    )

    result = abi.encode(
        ["bytes[]"],
        [(
            Web3.to_bytes(hexstr=f"0c49ccbe{decrease.hex()}"),
            Web3.to_bytes(hexstr=f"fc6f7865{collect.hex()}"),
            Web3.to_bytes(hexstr=f"49404b7c{unwrap.hex()}"),
            Web3.to_bytes(hexstr=f"df2ab5bb{sweep.hex()}")
        )]
    )

    fee = await chain.provider.eth.get_block("latest")
    base_fee = int(fee['baseFeePerGas'] * 1.01)
    priority_fee = await chain.provider.eth.max_priority_fee

    if priority_fee > base_fee:
        base_fee = priority_fee

    tx = {
        "to": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "data": f"0xac9650d8{result.hex()}",
        "chainId": chain.chain_id,
        "nonce": await chain.provider.eth.get_transaction_count(wallet.address),
        "from": wallet.address,
        "maxFeePerGas": base_fee,
        "maxPriorityFeePerGas": priority_fee,
        "gas": 0
    }

    gas_limit = await chain.provider.eth.estimate_gas(tx)
    tx.update({'gas': gas_limit})

    signed_txn = chain.provider.eth.account.sign_transaction(tx, private_key=wallet.private_key)
    send_txn = await chain.provider.eth.send_raw_transaction(signed_txn.rawTransaction)
    tx_receipt = await chain.provider.eth.wait_for_transaction_receipt(send_txn)

    if tx_receipt['status'] == 1:
        logger.success(f"{wallet.address}: Removed position | {tx_receipt['transactionHash'].hex()}")

        # with open("files/result.txt", "a") as f:
        #     f.write(f"{wallet.address}: {round(amount / (10 ** token_in.decimals), 2)} {token_in.symbol} -> {round((balance - min_amount_out) / (10 ** token_out.decimals), int(token_out.decimals / 3))} {token_out.symbol} [{token_in.chain.name}] | {tx_receipt['transactionHash'].hex()}\n")

        await asyncio.sleep(random.randint(5, 10))
        return

    else:
        logger.error(f"Not removed {wallet.address} [{tx}: {tx_receipt}]")
        raise Exception(f"Not removed {wallet.address} [{tx}: {tx_receipt}]")


async def rebalance_position(wallet: Wallet, chain: Network, eth_amount: float) -> None:
    await remove_liquidity(wallet, chain)

    if await chain.provider.eth.get_balance(wallet.address) / (10 ** 18) < eth_amount:
        delta = (eth_amount * 1.01) - await chain.provider.eth.get_balance(wallet.address) / (10 ** 18)
        amount = round(await get_price("ETH") * delta, 2)
        logger.debug(f"delta: {delta}, amount: {amount}")

        await sushi_swap(wallet, Arbitrum_USDC, Arbitrum_ETH, amount)

    if not await balance_enaugh(wallet, eth_amount, Arbitrum_USDC):
        await sushi_swap(wallet, Arbitrum_ETH, Arbitrum_USDC, eth_amount)

    await create_position(wallet, chain, eth_amount)


async def position_in_safe(wallet: Wallet):
    pos_range = wallet.position["high_price"] - wallet.position["low_price"]
    safe_limit = pos_range / 100 * position_safe_limit
    eth_price = await get_price("ETH")

    if wallet.position["low_price"] + safe_limit < eth_price < wallet.position["high_price"] - safe_limit:
        logger.success(f"Position in safe range: {wallet.position['low_price'] + safe_limit} <> {wallet.position['high_price'] - safe_limit}, current ETH price: {eth_price} USD")
        return True

    else:
        logger.warning(f"Position out safe range: {wallet.position['low_price'] + safe_limit} <> {wallet.position['high_price'] - safe_limit}, current ETH price: {eth_price} USD, rebalancing ...")
        return False


