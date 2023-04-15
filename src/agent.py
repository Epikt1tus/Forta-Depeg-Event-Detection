from __future__ import annotations
import asyncio
import json
import forta_agent
from forta_agent import get_json_rpc_url
from web3 import Web3
from src.db.db_utils import db_utils
from src.db.controller import init_async_db
from src.findings import SmartPriceChangesFindings
from src.utils import get_protocols_by_chain, extract_argument, get_token_name
from src.forecaster import forecast
from src.config import test_mode, history_capacity, minimal_capacity_to_forecast, critical_enable, high_enable, \
    medium_enable, low_enable, debug_logs_enabled

tokens_to_monitor = [
    # Ethereum
    {"symbol": "USDC", "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"},
    {"symbol": "DAI", "address": "0x6B175474E89094C44Da98b954EedeAC495271d0F"},
    {"symbol": "USDT", "address": "0xdAC17F958D2ee523a2206206994597C13D831ec7"},
    {"symbol": "stETH", "address": "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"},
    {"symbol": "WBTC", "address": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"}
    # Avalanche, Fantom, BSC, Arbitrum, Optimism, Polygon
]

swap_v2_abi = {
    "anonymous": False,
    "inputs": [
        {"indexed": True, "internalType": "address",
            "name": "sender", "type": "address"},
        {"indexed": False, "internalType": "uint256",
            "name": "amount0In", "type": "uint256"},
        {"indexed": False, "internalType": "uint256",
            "name": "amount1In", "type": "uint256"},
        {"indexed": False, "internalType": "uint256",
            "name": "amount0Out", "type": "uint256"},
        {"indexed": False, "internalType": "uint256",
            "name": "amount1Out", "type": "uint256"},
        {"indexed": True, "internalType": "address",
            "name": "to", "type": "address"},
    ],
    "name": "Swap",
    "type": "event",
}

swap_v3_abi = {
    "anonymous": False,
    "inputs": [
        {"indexed": False, "internalType": "int256",
            "name": "amount0", "type": "int256"},
        {"indexed": False, "internalType": "int256",
            "name": "amount1", "type": "int256"},
        {"indexed": True, "internalType": "address",
            "name": "sender", "type": "address"},
        {"indexed": True, "internalType": "address",
            "name": "recipient", "type": "address"},
        {"indexed": False, "internalType": "uint160",
            "name": "sqrtPriceX96After", "type": "uint160"},
        {"indexed": False, "internalType": "uint128",
            "name": "liquidity", "type": "uint128"},
        {"indexed": False, "internalType": "int24",
            "name": "tick", "type": "int24"},
    ],
    "name": "Swap",
    "type": "event",
}


global blocks_counter
global known_pools

known_pools = {}


initialized = False

web3 = Web3(Web3.HTTPProvider(get_json_rpc_url()))
chain_id = web3.eth.chain_id
protocols = get_protocols_by_chain(chain_id)

with open("./src/abi/pool_abi.json", 'r') as abi_file:  # get abi from the file
    pool_abi = json.load(abi_file)

with open("./src/abi/pool_v2_abi.json", 'r') as abi_v2_file:  # get abi from the file
    pool_v2_abi = json.load(abi_v2_file)

with open("./src/abi/token_abi.json", 'r') as abi_file:  # get abi from the file
    erc20_abi = json.load(abi_file)

swap_abi = next((x for x in pool_abi if x.get('name', "") == "Swap"), None)
swap_v2_abi = next(
    (x for x in pool_v2_abi if x.get('name', "") == "Swap"), None)


async def my_initialize(block_event: forta_agent.block_event.BlockEvent):
    """
    This function is initialize pattern, that is used instead the default Forta's initialize() because the block number
    is needed for the initialization
    @param block_event: block event received from the handle_block
    """
    global initialized
    global blocks_counter
    global known_pools

    # initialize database tables
    swaps_table, pools_table, future_table = await init_async_db(test_mode)
    print("Swaps table:", swaps_table)
    print("Pools table:", pools_table)
    print("Future table:", future_table)

    db_utils.set_tables(swaps_table, pools_table, future_table)
    print("DBUtils swaps:", db_utils.get_swaps())
    print("DBUtils pools:", db_utils.get_pools())
    print("DBUtils future:", db_utils.get_future())

    # if the database is not empty (in case the agent was restarted) we need to clear the old blocks firstly
    await clean_db(block_event.block_number)

    # we will count the blocks since agent's start
    blocks_counter = 0

    # export known pools from the database to the variable
    known_pools = {}
    pools = await pools_table.get_all_rows()

    # and count how many data about this pool we have
    for pool in pools:
        amount = len(await swaps_table.get_all_rows_by_criteria({'pool_contract': pool.pool_contract}))
        known_pools = {**known_pools, **{pool.pool_contract: amount}}

    initialized = True


async def analyze_transaction(transaction_event: forta_agent.transaction_event.TransactionEvent):
    """
    This function is triggered by handle_transaction using function main(). It is responsible for the adding the
    swaps to the database and its analysis after. Also, this function will trigger the forecaster in case there is
    no forecasted values and the forecast is possible
    @param transaction_event: Transaction event received from handle_transaction()
    @return: Findings
    """
    findings = []

    global known_pools

    # get the database tables
    swaps = db_utils.get_swaps()
    pools = db_utils.get_pools()
    future = db_utils.get_future()
    print(pools)
    print(swaps)
    print(future)

    # Find swap events in the transaction
    for event in [*transaction_event.filter_log(json.dumps(swap_v2_abi)),
                  *transaction_event.filter_log(json.dumps(swap_v3_abi))]:

        # Check if the pool's tokens match the ones in the tokens_to_monitor list
        pool = await pools.get_row_by_criteria({'pool_contract': event.address})
        if pool is not None:
            token0_address = pool.token0
            token1_address = pool.token1
            if not any(token["address"].lower() == token0_address.lower() or token["address"].lower() == token1_address.lower() for token in tokens_to_monitor):
                continue

        # add the pool to the database if we didn't know it yet
        if event.address not in list(known_pools.keys()):
            pool_contract = web3.eth.contract(
                address=Web3.toChecksumAddress(event.address), abi=pool_abi)

            # try to get the tokens addresses of the pool
            try:
                token0 = pool_contract.functions.token0().call(
                    block_identifier=int(transaction_event.block_number))
                token1 = pool_contract.functions.token1().call(
                    block_identifier=int(transaction_event.block_number))
            except Exception as e:
                if debug_logs_enabled:
                    print('INFO: Pool contract does not have token0 or token1 function')
                continue

            known_pools = {**known_pools, **{event.address: 0}}
            await pools.paste_row({'pool_contract': event.address, 'token0': token0, 'token1': token1})

        # get the amounts of the swap
        amount0 = extract_argument(event, "amount0")
        amount1 = extract_argument(event, "amount1")

        if not amount0 and not amount1:
            amount0In = abs(extract_argument(event, "amount0In"))
            amount1In = abs(extract_argument(event, "amount1In"))
            amount0Out = abs(extract_argument(event, "amount0Out"))
            amount1Out = abs(extract_argument(event, "amount1Out"))

            amount0 = amount0In if amount0In != 0 else amount0Out
            amount1 = amount1In if amount1In != 0 else amount1Out

        amount0 = abs(amount0)
        amount1 = abs(amount1)

        # calculate prices
        try:
            price0 = amount0 / amount1
            price1 = amount1 / amount0
        except Exception as e:
            if debug_logs_enabled:
                print(e)
            continue

        # for the forecasting purposes we prefer big values but not something like 0.0000....00001
        price = price0 if price0 > price1 else price1

        # add the swap to th db
        await swaps.paste_row(
            {'timestamp': transaction_event.timestamp, 'block': transaction_event.block_number,
             'pool_contract': event.address,
             'amount0': str(amount0), 'amount1': str(amount1), 'price': price})

        known_pools[event.address] = known_pools[event.address] + 1

        # since we have forecasted value for each hour, we need to calculate the timestamp rounded for the hour
        hourly_timestamp = transaction_event.block.timestamp - \
            transaction_event.block.timestamp % 60
        # get these rows from the database
        future_rows = await future.get_all_rows_by_criteria({'timestamp': hourly_timestamp})
        # and extract the estimation for the current pool
        future_row = None
        if future_rows:
            for fr in future_rows:
                if fr.pool_contract == event.address:
                    future_row = fr
                    break

        # if there is no estimation in the database but the capacity is big enough to calculate it then we need to
        # trigger the forecaster
        if not future_row and known_pools[event.address] > minimal_capacity_to_forecast:
            await forecast(event.address)

            # and try to get the forecasted values again
            future_rows = await future.get_all_rows_by_criteria({'timestamp': hourly_timestamp})
            future_row = None
            if future_rows:
                for fr in future_rows:
                    if fr.pool_contract == event.address:
                        future_row = fr
                        break

        if future_row:

            # we need to determine how volatile the protocol is
            uncertainty = (future_row.price_upper -
                           future_row.price_lower) if future_row else None

            error = abs(price - future_row.price)

            if debug_logs_enabled:
                print(f'INFO: Pool: {event.address}\n'
                      f'INFO: Real price: {price}\n'
                      f'INFO: Excepted price upper: {future_row.price_upper}\n'
                      f'INFO: Excepted price lower: {future_row.price_lower}\n'
                      f'INFO: Excepted price: {future_row.price}')

            if error > 4 * uncertainty + 0.05 and critical_enable:
                pool = await pools.get_row_by_criteria({'pool_contract': event.address})
                name0 = get_token_name(Web3.toChecksumAddress(
                    pool.token0), erc20_abi, web3)
                name1 = get_token_name(Web3.toChecksumAddress(
                    pool.token1), erc20_abi, web3)

                findings.append(SmartPriceChangesFindings.critical(protocols, transaction_event.to, event.address,
                                                                   future_row.price,
                                                                   price, transaction_event.hash, name0, name1))
            elif error > 3 * uncertainty + 0.05 and high_enable:
                pool = await pools.get_row_by_criteria({'pool_contract': event.address})
                name0 = get_token_name(Web3.toChecksumAddress(
                    pool.token0), erc20_abi, web3)
                name1 = get_token_name(Web3.toChecksumAddress(
                    pool.token1), erc20_abi, web3)

                findings.append(SmartPriceChangesFindings.high(protocols, transaction_event.to, event.address,
                                                               future_row.price,
                                                               price, transaction_event.hash, name0, name1))
            elif error > 2 * uncertainty + 0.05 and medium_enable:
                pool = await pools.get_row_by_criteria({'pool_contract': event.address})
                name0 = get_token_name(Web3.toChecksumAddress(
                    pool.token0), erc20_abi, web3)
                name1 = get_token_name(Web3.toChecksumAddress(
                    pool.token1), erc20_abi, web3)

                findings.append(SmartPriceChangesFindings.medium(protocols, transaction_event.to, event.address,
                                                                 future_row.price,
                                                                 price, transaction_event.hash, name0, name1))
            elif (price > future_row.price_upper or price < future_row.price_lower) and low_enable:
                pool = await pools.get_row_by_criteria({'pool_contract': event.address})
                name0 = get_token_name(Web3.toChecksumAddress(
                    pool.token0), erc20_abi, web3)
                name1 = get_token_name(Web3.toChecksumAddress(
                    pool.token1), erc20_abi, web3)

                findings.append(SmartPriceChangesFindings.low(protocols, transaction_event.to, event.address,
                                                              future_row.price,
                                                              price, transaction_event.hash, name0, name1))

    return findings


async def analyze_blocks(block_event: forta_agent.block_event.BlockEvent) -> None:
    """
    This function is triggered by handle_block using function main(). It is responsible for clean the database
    every 1k blocks.
    @param block_event: Block event received from handle_block()
    @return:
    """
    global blocks_counter

    # clean the database every 1k blocks
    blocks_counter += 1
    if blocks_counter > 1000:
        await clean_db(block_event.block_number)
        blocks_counter = 0


async def clean_db(block_number: int):
    """
    this function removes old rows from the database
    @param block_number:
    @return:
    """

    swaps = db_utils.get_swaps()

    await asyncio.gather(
        swaps.delete_old(block_number, history_capacity),
    )


async def main(event: forta_agent.transaction_event.TransactionEvent | forta_agent.block_event.BlockEvent):
    """
    This function is used to start logic functions in the different threads and then gather the findings
    """
    global initialized
    global blocks_counter

    if isinstance(event, forta_agent.transaction_event.TransactionEvent):
        return await asyncio.gather(
            analyze_transaction(event),
        )
    else:
        if not initialized:
            await my_initialize(event)
        await asyncio.gather(
            analyze_blocks(event),
        )
        return []


def provide_handle_transaction():
    """
    This function is just a wrapper for the handle_transaction()
    @return:
    """

    async def wrapped_handle_transaction(transaction_event):
        findings = await main(transaction_event)
        return [finding for finding_group in findings for finding in finding_group]

    return wrapped_handle_transaction


def provide_handle_block():
    """
    This function is just a wrapper for the handle_block()
    @return:
    """

    def wrapped_handle_block(block_event: forta_agent.block_event.BlockEvent) -> list:
        return [finding for findings in asyncio.run(main(block_event)) for finding in findings]

    return wrapped_handle_block


real_handle_transaction = provide_handle_transaction()


def handle_transaction(transaction_event: forta_agent.transaction_event.TransactionEvent):
    """
    This function is used by Forta SDK
    @param transaction_event: forta_agent.transaction_event.TransactionEvent
    @return:
    """
    return real_handle_transaction(transaction_event)


real_handle_block = provide_handle_block()


def handle_block(block_event: forta_agent.block_event.BlockEvent):
    """
    This function is used by Forta SDK
    @param block_event: forta_agent.block_event.BlockEvent
    @return:
    """
    return real_handle_block(block_event)
