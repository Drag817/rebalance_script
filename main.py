import os
import random
import asyncio
import traceback
from getpass import getpass

from loguru import logger
from aiohttp import ClientResponseError

from networks import Arbitrum
from utils import load_and_decrypt_wallets, valid_config
from files.config import wallets_file, shuffle_wallets, position_total_amount_ETH, check_interval
from contracts import Arbitrum_USDC, Arbitrum_ETH, Arbitrum_uniswap_positions
from tasks import (
    create_position, balance_enaugh, in_position, sushi_swap, rebalance_position, position_in_safe,
    valid_config_position_amount
)


logger.add(
    "logs/log.txt",
    level="INFO",
    rotation="00:00",
    retention="1w"
)


async def gather_tasks(wallets):
    tasks = []

    for wallet in wallets:
        task = asyncio.create_task(main_process(wallet))
        tasks.append(task)

    await asyncio.wait(tasks)


async def main_process(wallet):
    if not await valid_config_position_amount(wallet, Arbitrum):
        return

    while True:
        try:
            if not await in_position(wallet, Arbitrum_uniswap_positions):
                if not await balance_enaugh(wallet, position_total_amount_ETH, Arbitrum_USDC):
                    await sushi_swap(wallet, Arbitrum_ETH, Arbitrum_USDC, position_total_amount_ETH / 2)

                await create_position(wallet, Arbitrum, position_total_amount_ETH / 2)
                continue

            if not await position_in_safe(wallet):
                await rebalance_position(wallet, Arbitrum, position_total_amount_ETH / 2)
                # await send_status("Position rebalanced ...", wallet)

            logger.info(f"{wallet.address}: Waiting for next check in {check_interval} minutes ...")
            await asyncio.sleep(check_interval * 60)

        except ClientResponseError as cre:
            logger.warning(f"{wallet.address}: {type(cre).__name__}: {cre}")
            Arbitrum.change_rpc() # TODO: abstract
            await asyncio.sleep(random.randint(1, 5))

        except Exception as e:
            traceback.print_exc()
            logger.error(f"{wallet.address}: {type(e).__name__}: {e.args}")
            # await send_status(f"\n{type(e).__name__}:{e.args}\n\n{traceback.print_exc()}\n\n{wallet.address}")
            break


async def main(wallets_password):
    if not await valid_config():
        return

    wallets = load_and_decrypt_wallets(
        wallets_file,
        password=wallets_password,
        shuffle=shuffle_wallets
    )

    if len(wallets) == 0:
        logger.error("There is no wallets!")
        raise Exception("There is no wallets!")

    logger.info(f"Total wallets: {len(wallets)}")
    await gather_tasks(wallets)


if __name__ == "__main__":
    if os.getenv("PASS"):
        password = os.getenv("PASS").encode()
    else:
        password = getpass('Enter password: ').encode()

    logger.info(f"Password is: {password}")

    asyncio.run(main(password))
