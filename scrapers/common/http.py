"""Cliente HTTP "educado": User-Agent identificable, robots.txt, pausas y reintentos.

Reglas de cortesía que aplica:
- Consulta robots.txt una vez por host y se niega a pedir rutas no permitidas.
- Espera un tiempo aleatorio entre ``min_delay`` y ``max_delay`` entre peticiones.
- Reintenta con backoff exponencial solo errores transitorios (timeouts, 5xx).
- Ante 403/429 o una página de challenge anti-bot NO reintenta ni intenta evadir:
  lanza ``BlockedError`` para que la corrida termine limpiamente.
"""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests

from scrapers import __version__

logger = logging.getLogger(__name__)

def _default_user_agent() -> str:
    """UA con URL de contacto: el repo en GitHub Actions, o SCRAPER_CONTACT_URL en local."""
    contact = os.getenv("SCRAPER_CONTACT_URL")
    if not contact and os.getenv("GITHUB_REPOSITORY"):
        contact = f"{os.getenv('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ['GITHUB_REPOSITORY']}"
    contact = contact or "local-run"
    return f"gt-house-market/{__version__} (+{contact}; portfolio data project)"


DEFAULT_USER_AGENT = _default_user_agent()

# Marcadores de páginas de challenge (Cloudflare y similares).
_CHALLENGE_MARKERS = ("Just a moment...", "cf-chl", "challenges.cloudflare.com", "Attention Required!")
_RETRYABLE_STATUS = {500, 502, 503, 504}


class ScraperError(Exception):
    """Error base del scraper."""


class DisallowedByRobotsError(ScraperError):
    """La URL está bloqueada por robots.txt."""


class BlockedError(ScraperError):
    """El sitio rechazó o desafió la petición (403/429/challenge). No se reintenta."""


@dataclass(frozen=True)
class HttpConfig:
    user_agent: str = DEFAULT_USER_AGENT
    min_delay: float = 5.0
    max_delay: float = 10.0
    timeout: float = 30.0
    max_retries: int = 3
    backoff_base: float = 5.0
    respect_robots: bool = True


class PoliteSession:
    """Envoltura de ``requests.Session`` con las reglas de cortesía del proyecto."""

    def __init__(self, config: HttpConfig | None = None) -> None:
        self.config = config or HttpConfig()
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": self.config.user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "es-GT,es;q=0.9",
            }
        )
        self._robots: dict[str, RobotFileParser] = {}
        self._last_request_at: float | None = None
        self.requests_made = 0

    # -- API pública -------------------------------------------------------

    def get(self, url: str) -> requests.Response:
        """GET con robots.txt, pausa y reintentos. Devuelve solo respuestas 2xx."""
        if self.config.respect_robots:
            self._check_robots(url)

        for attempt in range(1, self.config.max_retries + 1):
            self._throttle()
            started = time.monotonic()
            try:
                response = self._session.get(url, timeout=self.config.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                self._log_retry(url, attempt, f"{type(exc).__name__}: {exc}")
                self._backoff(attempt)
                continue
            finally:
                self.requests_made += 1

            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info(
                "GET %s -> %s (%d bytes, %d ms)", url, response.status_code, len(response.content), elapsed_ms
            )

            if self._is_blocked(response):
                raise BlockedError(f"{url} respondió {response.status_code} (bloqueo o challenge anti-bot)")
            if response.status_code in _RETRYABLE_STATUS:
                self._log_retry(url, attempt, f"HTTP {response.status_code}")
                self._backoff(attempt)
                continue
            response.raise_for_status()
            return response

        raise ScraperError(f"{url}: se agotaron {self.config.max_retries} intentos")

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> PoliteSession:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- Internos ----------------------------------------------------------

    def _check_robots(self, url: str) -> None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        parser = self._robots.get(origin)
        if parser is None:
            parser = RobotFileParser()
            robots_url = urljoin(origin, "/robots.txt")
            try:
                resp = self._session.get(robots_url, timeout=self.config.timeout)
            except requests.RequestException as exc:
                raise ScraperError(f"No se pudo leer {robots_url}: {exc}") from exc
            if resp.status_code in (401, 403, 429):
                raise BlockedError(f"{robots_url} respondió {resp.status_code}; no se continúa")
            if resp.status_code >= 400:
                # Convención de robots.txt: 4xx = sin restricciones; 5xx = asumir prohibido.
                parser.parse([] if resp.status_code < 500 else ["User-agent: *", "Disallow: /"])
            else:
                parser.parse(resp.text.splitlines())
            logger.info("robots.txt de %s leído (HTTP %s)", origin, resp.status_code)
            self._robots[origin] = parser
        if not parser.can_fetch(self.config.user_agent, url):
            raise DisallowedByRobotsError(f"robots.txt no permite {url}")

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            wait = random.uniform(self.config.min_delay, self.config.max_delay)
            remaining = wait - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                logger.debug("Pausa de %.1f s", remaining)
                time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _backoff(self, attempt: int) -> None:
        if attempt < self.config.max_retries:
            time.sleep(self.config.backoff_base * 2 ** (attempt - 1))

    def _log_retry(self, url: str, attempt: int, reason: str) -> None:
        logger.warning("Intento %d/%d falló para %s: %s", attempt, self.config.max_retries, url, reason)

    @staticmethod
    def _is_blocked(response: requests.Response) -> bool:
        if response.status_code in (403, 429):
            return True
        if response.status_code == 503 or response.status_code < 300:
            head = response.text[:5000]
            return any(marker in head for marker in _CHALLENGE_MARKERS)
        return False
