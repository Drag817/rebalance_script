import requests
import asyncio
from loguru import logger

from web3 import Web3
from web3.eth import AsyncEth
from web3.middleware import async_geth_poa_middleware


class Network(Web3):
    def __init__(
            self,
            url: str | list,
            chain_id: int | None = None,
            name: str | None = None,
            symbol: str | None = None
    ):

        super().__init__()
        if isinstance(url, str):
            self.url = url
            self.urls = [self.url]

        else:
            self.urls = url
            self.url = self.urls[0]

        self.provider = Web3(
            provider=Web3.AsyncHTTPProvider(
                endpoint_uri=self.url,
                # request_kwargs={'proxy': self.proxy, 'headers': self.headers}
            ),
            modules={'eth': (AsyncEth,)},
            middlewares=[]
        )
        self.chain_id = chain_id if chain_id else asyncio.run(self.provider.eth.chain_id)
        self.name = name if name else self.get_name()
        self.symbol = symbol if symbol else self.get_name(symbol=True)
        self.provider.middleware_onion.inject(async_geth_poa_middleware, layer=0)

    def __str__(self) -> str:
        return f"{self.name}"

    def get_name(self, symbol: bool | None = False) -> str:
        res = requests.get('https://chainid.network/chains.json')
        for chain in res.json():
            if chain['chainId'] == self.chain_id:
                if symbol:
                    return chain['nativeCurrency']['symbol']
                else:
                    return chain['name']

    def change_rpc(self) -> None:
        if len(self.urls) == 1:
            logger.warning(f"There is nothing to change")

        else:
            next_index = self.urls.index(self.url) + 1
            self.url = self.urls[next_index] if next_index != len(self.urls) else self.urls[0]

            self.provider = Web3(
                provider=Web3.AsyncHTTPProvider(
                    endpoint_uri=self.url,
                    # request_kwargs={'proxy': self.proxy, 'headers': self.headers}
                ),
                modules={'eth': (AsyncEth,)},
                middlewares=[]
            )

            self.provider.middleware_onion.inject(async_geth_poa_middleware, layer=0)

        return


Etherium = Network(
    url="https://rpc.mevblocker.io",
    chain_id=1,
    name="Etherium",
    symbol="ETH"
)

Arbitrum = Network(
    url=["https://arbitrum.meowrpc.com", "https://api.zan.top/arb-one"],
    chain_id=42161,
    name="Arbitrum",
    symbol="ETH"
)
