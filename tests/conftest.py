import pytest

from binance_futures import app


@pytest.fixture()
def client():
    app.config.update({
        "TESTING": True,
    })
    with app.test_client() as client:
        yield client
