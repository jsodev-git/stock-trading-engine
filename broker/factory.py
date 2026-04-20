"""브로커 팩토리. 봇의 broker 이름으로 적절한 구현체를 생성한다."""
from __future__ import annotations

from .base import AccountType, BaseBroker, BrokerName, Market
from .kis import KISBroker
from .kiwoom import KiwoomBroker

_REGISTRY: dict[BrokerName, type[BaseBroker]] = {
    BrokerName.KIWOOM: KiwoomBroker,
    BrokerName.KIS: KISBroker,
}


def get_broker(
    name: str | BrokerName,
    market: str | Market = Market.KR,
    account_type: str | AccountType = AccountType.MOCK,
) -> BaseBroker:
    broker_enum = BrokerName(name) if isinstance(name, str) else name
    market_enum = Market(market) if isinstance(market, str) else market
    acct_enum = AccountType(account_type) if isinstance(account_type, str) else account_type

    cls = _REGISTRY.get(broker_enum)
    if cls is None:
        raise ValueError(f"지원하지 않는 브로커: {broker_enum}")
    return cls(market=market_enum, account_type=acct_enum)
