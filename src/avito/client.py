from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Optional
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


# Маркеры "антибот" для редиректов (Location)
_BLOCK_REDIRECT_MARKERS = (
    "captcha",
    "blocked",
    "security",
    "check",
    "verify",
    "antibot",
    "challenge",
)

# Статусы, которые обычно означают блок / ограничение
_BLOCK_STATUSES = (401, 403, 429)

# Большой пул User-Agent'ов для ротации при временных блокировках Avito.
# Список содержит популярные современные браузеры на разных ОС, чтобы снизить
# вероятность повторной блокировки из-за «примелькавшегося» fingerprint.
_USER_AGENT_POOL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13.6; rv:134.0) Gecko/20100101 Firefox/134.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edg/133.0.3065.69",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Opera/116.0.5366.71",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Brave/1.75.181 Chrome/132.0.0.0 Safari/537.36",
)


def _rotate_user_agent(session: aiohttp.ClientSession) -> str:
    """
    Выбирает случайный User-Agent из пула и применяет его к текущей сессии.

    Важно: стараемся не выбирать тот же самый UA повторно в рамках одного
    шага ретрая, чтобы действительно изменить «отпечаток» клиента.
    """
    current_ua = str(session.headers.get("User-Agent") or "")
    candidates = [ua for ua in _USER_AGENT_POOL if ua != current_ua] or list(_USER_AGENT_POOL)
    new_ua = random.choice(candidates)
    session.headers["User-Agent"] = new_ua
    return new_ua


def _jitter(a: float, b: float) -> float:
    return random.uniform(a, b)


def _safe_head(text: str, n: int = 240) -> str:
    t = text or ""
    t = t.replace("\n", " ").replace("\r", " ")
    return t[:n]


async def fetch_page(
    session: aiohttp.ClientSession,
    url: str,
    *,
    max_tries: int = 6,
    base_sleep_s: float = 6.0,
    max_sleep_s: float = 120.0,
) -> str:
    """
    Мягкая загрузка HTML:
    - allow_redirects=False, чтобы видеть 3xx и Location
    - 200 -> return html
    - 429/403/401 -> смена User-Agent + ожидание 60с + retry
    - 3xx -> логируем Location; часто это антибот. backoff+retry
    - сетевые/таймауты -> backoff+retry
    """
    last_status: Optional[int] = None
    last_location: Optional[str] = None

    for attempt in range(1, max_tries + 1):
        try:
            async with session.get(url, allow_redirects=False) as resp:
                status = resp.status
                last_status = status
                location = resp.headers.get("Location")
                last_location = location

                # читаем body для диагностики (полностью), но в лог пишем только head
                # (Avito HTML обычно не гигантский, а вам нужен парсинг)
                body = await resp.text(errors="ignore")
                body_head = _safe_head(body, 260)

                if status == 200:
                    return body

                # rate limit / forbidden -> backoff
                if status in _BLOCK_STATUSES:
                    # Для «жёстких» блокировок (429/403/401) используем фиксированную
                    # паузу в 60 секунд и одновременно меняем User-Agent.
                    # Это явно соответствует стратегии: «сменить fingerprint + выждать». 
                    rotated_ua = _rotate_user_agent(session)
                    sleep_s = 60.0
                    log.warning(
                        "fetch blocked: status=%s attempt=%s/%s action=rotate_user_agent wait=%.1fs url=%s location=%s new_user_agent=%r body_head=%r",
                        status, attempt, max_tries, sleep_s, url, location, rotated_ua, body_head,
                    )
                    if attempt == max_tries:
                        raise AvitoBlockedError(
                            f"Blocked with status {status} after retries url={url} location={location}"
                        )
                    await asyncio.sleep(sleep_s)
                    continue

                # redirects (anti-bot often)
                if status in (301, 302, 303, 307, 308):
                    loc = (location or "").strip()
                    loc_l = loc.lower()

                    looks_like_block = any(m in loc_l for m in _BLOCK_REDIRECT_MARKERS)

                    # иногда редирект может быть "легитимный", но нам всё равно нужен HTML целевой выдачи,
                    # а не промежуточный редирект — поэтому ретраим с бэкофом.
                    sleep_s = min(base_sleep_s * attempt + _jitter(0.8, 6.0), max_sleep_s)
                    rotated_ua = None
                    if looks_like_block:
                        # Если редирект похож на антибот-защиту, применяем ту же стратегию,
                        # что и для 429: смена User-Agent и ожидание 60 секунд.
                        rotated_ua = _rotate_user_agent(session)
                        sleep_s = 60.0
                    log.warning(
                        "fetch redirect: status=%s attempt=%s/%s sleep=%.1fs url=%s location=%s looks_like_block=%s new_user_agent=%r body_head=%r",
                        status, attempt, max_tries, sleep_s, url, loc, looks_like_block, rotated_ua, body_head,
                    )

                    if attempt == max_tries:
                        raise AvitoBlockedError(
                            f"Redirect {status} after retries url={url} location={loc}"
                        )
                    await asyncio.sleep(sleep_s)
                    continue

                # прочее — ошибка
                raise Exception(f"HTTP {status} for {url}. Body={body_head}")

        except AvitoBlockedError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            # сетевые/таймауты: тоже backoff
            sleep_s = min(base_sleep_s * attempt + _jitter(0.5, 5.0), max_sleep_s)
            log.warning(
                "fetch error: attempt=%s/%s sleep=%.1fs url=%s err=%s",
                attempt, max_tries, sleep_s, url, e,
            )
            if attempt == max_tries:
                raise AvitoBlockedError(
                    f"Network/timeout after retries url={url} last_status={last_status} last_location={last_location}"
                ) from e
            await asyncio.sleep(sleep_s)
            continue

    raise AvitoBlockedError(f"unreachable: url={url} last_status={last_status} last_location={last_location}")


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

        # Мягко, но устойчиво:
        # - limit=2 (не 1), чтобы не зависать на единственном сокете в редких сценариях
        # - keepalive_timeout удерживает соединение
        connector = aiohttp.TCPConnector(
            limit=2,
            limit_per_host=2,
            ttl_dns_cache=600,
            keepalive_timeout=30,
            enable_cleanup_closed=True,
            ssl=False,
        )

        return aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
            connector=connector,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )

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
            "anti-bot",
            "проверка на робота",
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

        # дополнительная страховка: иногда антибот приходит с 200
        if self._looks_like_protection(html_text):
            log.warning("Avito protection page (200) detected: source=%r page=%s url=%s", source, page, url)
            raise AvitoBlockedError("Protection HTML detected")

        cards = parse_catalog_page(html_text)

        if not cards:
            if self._looks_like_empty_results(html_text):
                log.info("Avito empty results page: source=%r page=%s url=%s", source, page, url)
            else:
                log.info("Avito page without cards (unknown reason): source=%r page=%s url=%s head=%r",
                         source, page, url, _safe_head(html_text, 220))

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

                if self._looks_like_protection(html_text):
                    log.warning("stop fetch_pages due to protection(200): source=%r page=%s url=%s", source, p, url)
                    raise AvitoBlockedError(f"Protection page detected at page={p}")

                cards = parse_catalog_page(html_text)

                if not cards:
                    reason = "empty_results" if self._looks_like_empty_results(html_text) else "no_cards"
                    log.info("stop fetch_pages: reason=%s source=%r page=%s url=%s", reason, source, p, url)
                    break

                pages.append(cards)

                if p != self.cfg.max_pages:
                    delay = self.cfg.page_delay_s + _jitter(0.5, 2.0)
                    await asyncio.sleep(delay)

        return pages
