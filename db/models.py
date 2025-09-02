# models.py
from sqlalchemy import Column, Integer, String, DateTime, Boolean, BigInteger, ForeignKey, Float
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, relationship
from datetime import datetime

# Настройка асинхронного подключения к SQLite3
DB_URL = "sqlite+aiosqlite:///db/database.db"
engine = create_async_engine(DB_URL)  # Асинхронный движок SQLAlchemy
Session = async_sessionmaker(expire_on_commit=False, bind=engine)  # Фабрика сессий


class Base(DeclarativeBase, AsyncAttrs):
    """Базовый класс для декларативных моделей с поддержкой асинхронных атрибутов"""
    pass


class Wallet(Base):
    """Модель для хранения данных по кошелькам"""
    __tablename__ = "wallet"

    id = Column(Integer, primary_key=True)
    address = Column(String, unique=True, nullable=False)
    token = Column(String, nullable=False)  # btc, eth, ton, tron
    time_add = Column(DateTime, default=datetime.utcnow)

    balance = relationship("Balance", back_populates="wallet")


class Balance(Base):
    """Модель для хранения данных по балансам кошельков"""
    __tablename__ = "balance"

    id = Column(Integer, primary_key=True)
    wallet_id = Column(Integer, ForeignKey("wallet.id"))
    coin = Column(String, nullable=False)
    amount = Column(Float, default=0.0)
    price = Column(Float, default=0.0)  # Стоимость в USD
    time_check = Column(DateTime, default=datetime.utcnow)

    wallet = relationship("Wallet", back_populates="balance")


class User(Base):
    """Модель для хранения данных пользователей"""
    __tablename__ = "user"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, unique=True, nullable=False)
    is_active = Column(Boolean, default=False)
    time_add = Column(DateTime, default=datetime.utcnow)


async def create_tables():
    """Создает таблицы в базе данных"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
