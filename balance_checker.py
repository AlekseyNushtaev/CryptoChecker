import asyncio
from datetime import datetime, timedelta

import requests
from sqlalchemy import select, desc
from db.models import Session, Wallet, Balance, User
from bot import bot
from config import ADMIN_IDS, ETH_TOKEN


def get_price(coin):
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin}&vs_currencies=usd"
        response = requests.get(url)
        data = response.json()
        price = data[coin]['usd']
        return price
    except:
        return 0


async def get_balance_btc(address):
    url = f"https://blockchain.info/balance?active={address}"

    try:
        response = requests.get(url)
        data = response.json()

        # Проверяем наличие данных об адресе
        if address not in data:
            print("Адрес не найден или ошибка в ответе API")
            return 0, 'btc', 0

        balance_satoshi = data[address]['final_balance']
        # Конвертируем в BTC (1 BTC = 100,000,000 сатоши)
        balance_btc = balance_satoshi / 100000000
        price = get_price('bitcoin') * balance_btc

        return balance_btc, 'btc', price

    except Exception as e:
        print(f"Ошибка при получении баланса BTC: {e}")
        return 0, 'btc', 0


async def get_balance_ton(address):
    url = f"https://toncenter.com/api/v2/getAddressInformation?address={address}"

    try:
        response = requests.get(url)
        data = response.json()

        balance_nano = int(data['result']['balance'])
        # Конвертируем в TON (1 TON = 1e9 нанотон)
        balance_ton = balance_nano / 1e9
        price = get_price('the-open-network') * balance_ton  # Используем корректный ID для TON

        return balance_ton, 'ton', price

    except Exception as e:
        print(f"Ошибка при получении баланса TON: {e}")
        return 0, 'ton', 0


async def get_balance_eth(address):
    url = f"https://api.etherscan.io/api?module=account&action=balance&address={address}&tag=latest&apikey={ETH_TOKEN}"

    try:
        response = requests.get(url)
        data = response.json()

        if data['status'] == '1' and data['message'] == 'OK':
            balance_wei = int(data['result'])
            # Конвертируем в ETH (1 ETH = 10^18 wei)
            balance_eth = balance_wei / 10 ** 18
            price = get_price('ethereum') * balance_eth  # Используем ID Ethereum

            return balance_eth, 'eth', price
        else:
            print(f"Ошибка API Etherscan: {data['message']}")
            return 0, 'eth', 0

    except Exception as e:
        print(f"Ошибка при получении баланса ETH: {e}")
        return 0, 'eth', 0


async def get_balance_usdt_tron(address):
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
        price = get_price('tether') * usdt_balance

        return usdt_balance, 'usdt', price

    except Exception as e:
        print(f"Ошибка при получении баланса USDT-TRON: {e}")
        return 0, 'usdt', 0


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

        for wallet in wallets:
            await asyncio.sleep(10)
            # Получаем баланс в зависимости от типа токена
            if wallet.token == 'btc':
                amount, coin, price = await get_balance_btc(wallet.address)
            elif wallet.token == 'eth':
                amount, coin, price = await get_balance_eth(wallet.address)
            elif wallet.token == 'ton':
                amount, coin, price = await get_balance_ton(wallet.address)
            elif wallet.token == 'tron':
                amount, coin, price = await get_balance_usdt_tron(wallet.address)
            else:
                continue

            # Сохраняем данные для отчета
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

            # Если это не первая проверка и баланс изменился
            if len(balances) > 1 and balances[0].amount != balances[1].amount:
                changes_detected = True
            elif len(balances) == 1:
                changes_detected = True

        await session.commit()

        # Формируем и отправляем сообщение, если есть изменения
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

            message += f"Общий баланс в USD - {total_balance:.2f}"

            # Отправляем сообщение всем админам
            for admin_id in ADMIN_IDS:
                await bot.send_message(admin_id, message)

            # Отправляем сообщение всем активным юзерам
            result = await session.execute(select(User).where(User.is_active == True))
            active_users = result.scalars().all()

            for user in active_users:
                try:
                    await bot.send_message(user.user_id, message)
                except:
                    # Если пользователь заблокировал бота
                    pass


async def periodic_balance_check():
    while True:
        start_time = datetime.now()
        await check_balances()  # Основная задача
        elapsed = datetime.now() - start_time  # Время выполнения задачи
        wait_time = max(timedelta(minutes=15) - elapsed, timedelta(0))  # Ждём оставшееся время
        await asyncio.sleep(wait_time.total_seconds())  # Ожидание до следующего цикла