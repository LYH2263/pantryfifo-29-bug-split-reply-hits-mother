import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例一个独立 tmp 数据库；startup 时按种子数据初始化。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(app) as c:
        yield c
