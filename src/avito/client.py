from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import aiohttp

from .parser import ParsedCard, parse_catalog_page

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AvitoClientConfig:
    city_slug: str
    max_pages: int
    page_delay_s: int
    timeout_s: int
    user_agent: str


class AvitoBlockedError(RuntimeError):
    pass


async def fetch_page(session: aiohttp.ClientSession, url: str) -> str:
    max_tries = 5
    base_sleep = 8

    for attempt in range(1, max_tries + 1):
        async with session.get(url, allow_redirects=True) as resp:
            status = resp.status

            if status == 200:
                return await resp.text()

            if status in (429, 403):
                sleep_s = base_sleep * (2 ** (attempt - 1)) + random.uniform(0.0, 2.0)
                sleep_s = min(sleep_s, 90.0)

                try:
                    await resp.read()
                except Exception:
                    pass

                log.warning(
                    "fetch blocked: status=%s attempt=%s/%s sleep=%.1fs url=%s",
                    status, attempt, max_tries, sleep_s, url
                )

                if attempt == max_tries:
                    raise AvitoBlockedError(f"Blocked with status {status} after retries")

                await asyncio.sleep(sleep_s)
                continue

            try:
                body = await resp.text()
            except Exception:
                body = ""
            raise Exception(f"HTTP {status} for {url}. Body={body[:200]}")

    raise Exception("unreachable")


class AvitoClient:
    """
    Два режима:
    - source = текст (старый): строим URL поиска по /{city}/noutbuki?q=...
    - source = URL (новый): берём URL как есть и добавляем/обновляем только p=...
    """

    def __init__(self, cfg: AvitoClientConfig) -> None:
        self.cfg = cfg

    @staticmethod
    def _is_url(source: str) -> bool:
        s = (source or "").strip().lower()
        return s.startswith("http://") or s.startswith("https://")

    def build_search_url(self, query: str, page: int) -> str:
        base = f"https://www.avito.ru/{self.cfg.city_slug}/noutbuki"
        parts = urlsplit(base)
        q = dict(parse_qsl(parts.query, keep_blank_values=True))

        q["cd"] = "1"
        q["s"] = "104"
        q["localPriority"] = "0"

        q["q"] = query
        q["p"] = str(page)

        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q, doseq=True), parts.fragment))

    def build_category_url(self, category_url: str, page: int) -> str:
        parts = urlsplit(category_url.strip())
        q = dict(parse_qsl(parts.query, keep_blank_values=True))
        q["p"] = str(page)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q, doseq=True), parts.fragment))

    def build_source_url(self, source: str, page: int) -> str:
        if self._is_url(source):
            return self.build_category_url(source, page)
        return self.build_search_url(source, page)

    def _make_session(self) -> aiohttp.ClientSession:
        timeout = aiohttp.ClientTimeout(total=self.cfg.timeout_s)
        headers = {
            "User-Agent": self.cfg.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
        }
        # мягко: один коннект, меньше подозрений
        connector = aiohttp.TCPConnector(limit=1, ttl_dns_cache=300, ssl=False)
        return aiohttp.ClientSession(timeout=timeout, headers=headers, connector=connector)

    @staticmethod
    def _looks_like_protection(html_text: str) -> bool:
        """
        Делать максимально точным: иначе пустые страницы можно перепутать с защитой.
        """
        low = (html_text or "").lower()

        markers = [
            "captcha",
            "recaptcha",
            "hcaptcha",
            "проверка безопасности",
            "подтвердите, что вы человек",
            "мы обнаружили подозрительную активность",
            "доступ ограничен",
            "robot check",
        ]
        return any(m in low for m in markers)

    @staticmethod
    def _looks_like_empty_results(html_text: str) -> bool:
        """
        Пустая выдача / конец страниц.
        """
        low = (html_text or "").lower()
        markers = [
            "ничего не найдено",
            "по вашему запросу ничего не найдено",
            "объявлений не найдено",
            "нет объявлений",
            "попробуйте изменить параметры поиска",
        ]
        return any(m in low for m in markers)

    async def fetch_page_cards_in_session(
        self,
        session: aiohttp.ClientSession,
        source: str,
        page: int,
    ) -> list[ParsedCard]:
        url = self.build_source_url(source, page)
        html_text = await fetch_page(session, url)

        cards = parse_catalog_page(html_text)

        # Логи причины (важно для отладки)
        if not cards:
            if self._looks_like_protection(html_text):
                log.warning("Avito protection detected: source=%r page=%s url=%s", source, page, url)
            elif self._looks_like_empty_results(html_text):
                log.info("Avito empty results page: source=%r page=%s url=%s", source, page, url)
            else:
                log.info("Avito page without cards (unknown reason): source=%r page=%s url=%s", source, page, url)

        log.info("Avito page parsed: source=%r page=%s cards=%s url=%s", source, page, len(cards), url)
        return cards

    async def fetch_pages(self, source: str) -> list[list[ParsedCard]]:
        """
        ВАЖНО:
        - Если страница пустая (cards=0) -> останавливаемся (break).
        - Если похоже на protection -> останавливаемся сразу (исключение),
          чтобы не долбить дальше страницы и не усугублять.
        """
        pages: list[list[ParsedCard]] = []

        async with self._make_session() as session:
            for p in range(1, self.cfg.max_pages + 1):
                url = self.build_source_url(source, p)
                html_text = await fetch_page(session, url)
                cards = parse_catalog_page(html_text)

                # 1) Protection -> стоп сразу
                if not cards and self._looks_like_protection(html_text):
                    log.warning(
                        "stop fetch_pages due to protection: source=%r page=%s url=%s",
                        source, p, url
                    )
                    raise AvitoBlockedError(f"Protection page detected at page={p}")

                # 2) Пусто -> стоп (конец/нет выдачи)
                if not cards:
                    reason = "empty_results" if self._looks_like_empty_results(html_text) else "no_cards"
                    log.info(
                        "stop fetch_pages: reason=%s source=%r page=%s url=%s",
                        reason, source, p, url
                    )
                    break

                pages.append(cards)

                if p != self.cfg.max_pages:
                    delay = self.cfg.page_delay_s + random.uniform(0.5, 2.0)
                    await asyncio.sleep(delay)

        return pages
