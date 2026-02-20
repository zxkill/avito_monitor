"""Тесты для устойчивого HTTP-клиента Avito с обработкой блокировок."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from src.avito.client import _rotate_user_agent, fetch_page


class _FakeResponse:
    """Минимальная async-response заглушка с интерфейсом aiohttp-ответа."""

    def __init__(self, *, status: int, body: str = "", headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def text(self, errors: str = "ignore") -> str:
        """Возвращает текст тела ответа, имитируя aiohttp API."""
        _ = errors  # Параметр нужен только для совместимости сигнатуры.
        return self._body

    async def __aenter__(self) -> "_FakeResponse":
        """Поддержка async with для корректной эмуляции сетевого запроса."""
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        """Исключения не подавляем, чтобы тесты ловили реальные ошибки."""
        _ = (exc_type, exc, tb)
        return False


class _FakeSession:
    """Минимальная сессия с очередью предопределённых HTTP-ответов."""

    def __init__(self, responses: list[_FakeResponse], user_agent: str = "UA-INITIAL") -> None:
        # Поддерживаем заголовки как обычный dict — этого достаточно для тестируемого кода.
        self.headers = {"User-Agent": user_agent}
        self._responses = responses
        self.calls: list[tuple[str, bool]] = []

    def get(self, url: str, allow_redirects: bool = False) -> _FakeResponse:
        """Возвращает следующий ответ из очереди, фиксируя параметры вызова."""
        self.calls.append((url, allow_redirects))
        if not self._responses:
            raise AssertionError("Неожиданный лишний вызов session.get() в тесте")
        return self._responses.pop(0)


class AvitoClientFetchPageTests(unittest.IsolatedAsyncioTestCase):
    """Проверяет ретраи при блокировках, ротацию UA и ожидание перед повтором."""

    def test_rotate_user_agent_replaces_header(self) -> None:
        """Ротация должна установить новый UA в заголовках сессии."""
        session = _FakeSession(responses=[], user_agent="UA-OLD")

        with patch("src.avito.client.random.choice", return_value="UA-NEW"):
            new_ua = _rotate_user_agent(session)

        self.assertEqual(new_ua, "UA-NEW")
        self.assertEqual(session.headers["User-Agent"], "UA-NEW")

    async def test_fetch_page_on_429_rotates_ua_waits_minute_and_retries(self) -> None:
        """При 429 клиент должен сменить UA, подождать 60 секунд и повторить запрос."""
        session = _FakeSession(
            responses=[
                _FakeResponse(status=429, body="too many requests"),
                _FakeResponse(status=200, body="ok-body"),
            ]
        )

        with patch("src.avito.client._rotate_user_agent", return_value="UA-ROTATED") as rotate_mock, patch(
            "src.avito.client.asyncio.sleep", new_callable=AsyncMock
        ) as sleep_mock:
            result = await fetch_page(session, "https://example.com", max_tries=3)

        self.assertEqual(result, "ok-body")
        rotate_mock.assert_called_once_with(session)
        sleep_mock.assert_awaited_once_with(60.0)

    async def test_fetch_page_on_protection_redirect_rotates_ua_and_waits_minute(self) -> None:
        """Антибот-редирект (captcha) должен запускать стратегию rotate + wait=60s."""
        session = _FakeSession(
            responses=[
                _FakeResponse(status=302, headers={"Location": "https://www.avito.ru/security/captcha"}, body="redirect"),
                _FakeResponse(status=200, body="ok-body"),
            ]
        )

        with patch("src.avito.client._rotate_user_agent", return_value="UA-ROTATED") as rotate_mock, patch(
            "src.avito.client.asyncio.sleep", new_callable=AsyncMock
        ) as sleep_mock:
            result = await fetch_page(session, "https://example.com", max_tries=3)

        self.assertEqual(result, "ok-body")
        rotate_mock.assert_called_once_with(session)
        sleep_mock.assert_awaited_once_with(60.0)

    async def test_fetch_page_on_regular_redirect_uses_backoff_without_ua_rotation(self) -> None:
        """Обычный редирект без маркеров блокировки не должен менять User-Agent."""
        session = _FakeSession(
            responses=[
                _FakeResponse(status=302, headers={"Location": "https://www.avito.ru/moskva/noutbuki"}, body="redirect"),
                _FakeResponse(status=200, body="ok-body"),
            ]
        )

        with patch("src.avito.client._rotate_user_agent") as rotate_mock, patch(
            "src.avito.client._jitter", return_value=0.0
        ), patch("src.avito.client.asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
            result = await fetch_page(session, "https://example.com", max_tries=3, base_sleep_s=6.0)

        self.assertEqual(result, "ok-body")
        rotate_mock.assert_not_called()
        sleep_mock.assert_awaited_once_with(6.0)


if __name__ == "__main__":
    unittest.main()
