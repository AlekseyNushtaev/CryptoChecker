import asyncio
from datetime import datetime, timedelta
from pprint import pprint

import requests
from sqlalchemy import select, desc
from db.models import Session, Wallet, Balance, User, CryptoFlow, Currency
from bot import bot
from config import ADMIN_IDS, ETH_TOKEN
from handlers import get_admin_keyboard


def get_price(coin):
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin}&vs_currencies=usd"
        response = requests.get(url)
        data = response.json()
        price = data[coin]['usd']
        return price
    except:
        return None


async def get_balance_btc(address, currency):
    url = f"https://blockchain.info/balance?active={address}"
    print(address)

    try:
        response = requests.get(url)
        data = response.json()

        # Проверяем наличие данных об адресе
        if address not in data:
            pprint(data)
            print(f"Адрес не найден или ошибка в ответе API - {address}")
            await bot.send_message(1012882762, f"Адрес не найден или ошибка в ответе API BTC - {address}")
            return None, None, None

        balance_satoshi = data[address]['final_balance']
        # Конвертируем в BTC (1 BTC = 100,000,000 сатоши)
        balance_btc = balance_satoshi / 100000000
        price = currency * balance_btc

        return balance_btc, 'btc', price

    except Exception as e:
        print(f"Ошибка при получении баланса BTC: {e}")
        await bot.send_message(1012882762, f"{address} - Ошибка при получении баланса BTC: {e}")
        return None, None, None


async def get_balance_ton(address, currency):
    url = f"https://toncenter.com/api/v2/getAddressInformation?address={address}"

    try:
        response = requests.get(url)
        data = response.json()

        balance_nano = int(data['result']['balance'])
        # Конвертируем в TON (1 TON = 1e9 нанотон)
        balance_ton = balance_nano / 1e9
        price = currency * balance_ton  # Используем корректный ID для TON

        return balance_ton, 'ton', price

    except Exception as e:
        print(f"Ошибка при получении баланса TON: {e}")
        await bot.send_message(1012882762, f"{address} - Ошибка при получении баланса TON: {e}")
        return None, None, None


async def get_balance_eth(address, currency):
    url = f"https://api.etherscan.io/api?module=account&action=balance&address={address}&tag=latest&apikey={ETH_TOKEN}"

    try:
        response = requests.get(url)
        data = response.json()

        if data['status'] == '1' and data['message'] == 'OK':
            balance_wei = int(data['result'])
            # Конвертируем в ETH (1 ETH = 10^18 wei)
            balance_eth = balance_wei / 10 ** 18
            price = currency * balance_eth  # Используем ID Ethereum

            return balance_eth, 'eth', price
        else:
            print(f"Ошибка API Etherscan: {data['message']}")
            await bot.send_message(1012882762, f"{address} - Ошибка API Etherscan: {data['message']}")
            return None, None, None

    except Exception as e:
        print(f"Ошибка при получении баланса ETH: {e}")
        await bot.send_message(1012882762, f"{address} - Ошибка при получении баланса ETH: {e}")
        return None, None, None


async def get_balance_usdt_tron(address, currency):
    # USDT contract address on TRON (для проверки в ответе)
    usdt_contract_address = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

    # API endpoint для получения информации об аккаунте
    url = f"https://apilist.tronscanapi.com/api/account?address={address}"

    try:
        response = requests.get(url)
        data = response.json()

        # Проверяем наличие данных об аккаунте
        if 'trc20token_balances' not in data:
            return 0, 'usdt', 0

        # Ищем USDT среди TRC20 токенов
        usdt_balance = 0
        for token in data['trc20token_balances']:
            if token['tokenId'] == usdt_contract_address:
                # Получаем баланс с учетом decimals
                usdt_balance = float(token['balance']) / (10 ** token['tokenDecimal'])
                break

        # Получаем цену USDT
        price = currency * usdt_balance

        return usdt_balance, 'usdt', price

    except Exception as e:
        print(f"Ошибка при получении баланса USDT-TRON: {e}")
        await bot.send_message(1012882762, f"{address} - Ошибка при получении баланса USDT-TRON: {e}")
        return None, None, None


async def check_balances():
    async with Session() as session:
        time_check = datetime.now()
        await asyncio.sleep(5)
        result = await session.execute(select(Wallet))
        wallets = result.scalars().all()

        changes_detected = False
        balances_by_token = {
            'btc': [],
            'eth': [],
            'ton': [],
            'tron': []
        }
        total_balance = 0.0
        total_inflow = 0.0  # Сумма поступлений
        total_outflow = 0.0  # Сумма выводов

        # Блок получения курсов монет
        currency_mapping = {
            'btc': 'bitcoin',
            'eth': 'ethereum',
            'ton': 'the-open-network',
            'tron': 'tether'  # Для TRON используем курс USDT
        }

        currencies = {}
        for coin_name, gecko_id in currency_mapping.items():
            price = get_price(gecko_id)
            await asyncio.sleep(10)

            async with Session() as temp_session:
                if price is not None:
                    # Обновляем курс в базе данных
                    result = await temp_session.execute(
                        select(Currency).where(Currency.coin == coin_name)
                    )
                    currency_record = result.scalar_one_or_none()

                    if currency_record:
                        currency_record.currency = price
                    else:
                        currency_record = Currency(coin=coin_name, currency=price)
                        temp_session.add(currency_record)

                    await temp_session.commit()
                    currencies[coin_name] = price
                else:
                    await bot.send_message(1012882762, f"Ошибка при получении курса {coin_name}")
                    # Берем последний курс из базы данных
                    result = await temp_session.execute(
                        select(Currency.currency)
                        .where(Currency.coin == coin_name)
                    )
                    last_currency = result.scalar_one_or_none()
                    currencies[coin_name] = last_currency if last_currency else 0.0

        currency_btc = currencies['btc']
        currency_eth = currencies['eth']
        currency_ton = currencies['ton']
        currency_tron = currencies['tron']

        for wallet in wallets:
            await asyncio.sleep(3)
            if wallet.token == 'btc':
                amount, coin, price = await get_balance_btc(wallet.address, currency_btc)
                if not amount:
                    continue
            elif wallet.token == 'eth':
                amount, coin, price = await get_balance_eth(wallet.address, currency_eth)
                if not amount:
                    continue
            elif wallet.token == 'ton':
                amount, coin, price = await get_balance_ton(wallet.address, currency_ton)
                if not amount:
                    continue
            elif wallet.token == 'tron':
                amount, coin, price = await get_balance_usdt_tron(wallet.address, currency_tron)
                if not amount:
                    continue
            else:
                continue

            balances_by_token[wallet.token].append((wallet.address, amount, price))
            total_balance += price

            # Сохраняем новый баланс
            balance = Balance(
                wallet_id=wallet.id,
                coin=wallet.token,
                amount=amount,
                price=price,
                time_check=time_check
            )
            session.add(balance)
            await session.commit()

            # Проверяем изменение баланса
            last_balance = await session.execute(
                select(Balance)
                .where(Balance.wallet_id == wallet.id)
                .order_by(desc(Balance.time_check))
                .limit(2)
            )
            balances = last_balance.scalars().all()

            if len(balances) > 1 and balances[0].amount != balances[1].amount:
                changes_detected = True
                # Вычисляем изменение баланса
                delta = balances[0].amount - balances[1].amount
                delta_price = balances[0].price - balances[1].price

                # Записываем изменение в CryptoFlow
                flow = CryptoFlow(
                    wallet_id=wallet.id,
                    amount=delta,
                    coin=wallet.token,
                    price=delta_price
                )
                session.add(flow)

                # Суммируем приток/отток
                if delta > 0:
                    total_inflow += delta_price
                else:
                    total_outflow += abs(delta_price)

            elif len(balances) == 1:
                changes_detected = True

        await session.commit()

        if changes_detected:
            message = ""
            for token in ['btc', 'eth', 'ton', 'tron']:
                if balances_by_token[token]:
                    message += f"{token}\n"
                    for address, amount, price in balances_by_token[token]:
                        if token != 'tron':
                            message += f"{address} - {amount} {token}\n"
                        else:
                            message += f"{address} - {amount} usdt\n"
                    message += "\n"

            message += f"Общий баланс в USD - {total_balance:.2f} $\n"

            if total_inflow > 0:
                message += f"Поступление - {total_inflow:.2f} $\n"
            if total_outflow > 0:
                message += f"Вывод - {total_outflow:.2f} $\n"

            for admin_id in ADMIN_IDS:
                await bot.send_message(admin_id, message, reply_markup=get_admin_keyboard())

            result = await session.execute(select(User).where(User.is_active == True))
            active_users = result.scalars().all()

            for user in active_users:
                try:
                    await bot.send_message(user.user_id, message)
                except:
                    pass


async def periodic_balance_check():
    while True:
        start_time = datetime.now()
        await check_balances()  # Основная задача
        elapsed = datetime.now() - start_time  # Время выполнения задачи
        wait_time = max(timedelta(minutes=5) - elapsed, timedelta(0))  # Ждём оставшееся время
        await asyncio.sleep(wait_time.total_seconds())  # Ожидание до следующего цикла