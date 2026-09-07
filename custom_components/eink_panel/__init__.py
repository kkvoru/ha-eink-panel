"""Lightweight E-Ink panel for old browsers."""

from __future__ import annotations

import asyncio
import hmac
from html import escape
import json
import logging
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlparse

from aiohttp import web
import voluptuous as vol

from homeassistant.components.http import HomeAssistantView
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

DOMAIN = "eink_panel"

CONF_ACCESS_KEY = "access_key"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_TEMPERATURE_ENTITY = "temperature_entity"
CONF_HUMIDITY_ENTITY = "humidity_entity"
CONF_LOCK_ENTITY = "lock_entity"
CONF_LOCK_LABEL = "lock_label"

_LOGGER = logging.getLogger(__name__)

ICON_CACHE_SECONDS = 30 * 60
MAX_ICON_BYTES = 512 * 1024


def _detected_image_type(body: bytes) -> str | None:
    """Detect only image formats suitable for the old browser."""
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    stripped = body.lstrip()
    if stripped.startswith(b"<svg") or (
        stripped.startswith(b"<?xml") and b"<svg" in stripped[:512]
    ):
        return "image/svg+xml"
    return None

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_ACCESS_KEY): vol.All(
                    cv.string,
                    vol.Length(min=12, max=128),
                    vol.Match(r"^[A-Za-z0-9_-]+$"),
                ),
                vol.Required(CONF_WEATHER_ENTITY): cv.entity_id,
                vol.Required(CONF_TEMPERATURE_ENTITY): cv.entity_id,
                vol.Required(CONF_HUMIDITY_ENTITY): cv.entity_id,
                vol.Required(CONF_LOCK_ENTITY): cv.entity_id,
                vol.Optional(CONF_LOCK_LABEL, default="Открыть домофон"): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def _valid_key(request: web.Request, expected: str) -> bool:
    supplied = request.query.get("key", "")
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _plain_value(state: Any) -> dict[str, str]:
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return {"value": "—", "unit": ""}

    value = state.state
    try:
        number = float(value)
        value = f"{number:.1f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        pass

    return {
        "value": value,
        "unit": str(state.attributes.get("unit_of_measurement", "")),
    }


def _forecast_number(value: Any) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
        return f"{number:.0f}"
    except (TypeError, ValueError):
        return str(value)


def _as_number(value: Any) -> float | None:
    """Return a numeric forecast value when possible."""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _decimal_number(value: Any) -> str:
    """Format a measurement compactly with a Russian decimal comma."""
    number = _as_number(value)
    if number is None:
        return "—"
    return f"{number:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _forecast_local_date(value: Any):
    """Parse a Home Assistant forecast datetime into the HA local date."""
    if not value:
        return None
    parsed = dt_util.parse_datetime(str(value))
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = dt_util.as_local(parsed)
    return parsed.date()


def _forecast_time(value: Any) -> str:
    """Return local HH:MM for a forecast entry."""
    if not value:
        return "--:--"
    parsed = dt_util.parse_datetime(str(value))
    if parsed is None:
        return "--:--"
    if parsed.tzinfo is not None:
        parsed = dt_util.as_local(parsed)
    return parsed.strftime("%H:%M")


def _forecast_weekday(value: Any) -> str:
    """Return a short Russian weekday for a forecast entry."""
    day_date = _forecast_local_date(value)
    if day_date is None:
        return ""
    return ("пн", "вт", "ср", "чт", "пт", "сб", "вс")[day_date.weekday()]


def _wind_direction(value: Any) -> str:
    """Convert a numeric wind bearing to a short Russian direction."""
    number = _as_number(value)
    if number is None:
        return str(value or "")
    directions = ("С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ")
    return directions[int((number % 360 + 22.5) // 45) % 8]


CONDITION_RU = {
    "clear-night": "Ясно",
    "cloudy": "Облачно",
    "exceptional": "Особые условия",
    "fog": "Туман",
    "hail": "Град",
    "lightning": "Гроза",
    "lightning-rainy": "Гроза с дождём",
    "partlycloudy": "Переменная облачность",
    "pouring": "Ливень",
    "rainy": "Дождь",
    "snowy": "Снег",
    "snowy-rainy": "Дождь со снегом",
    "sunny": "Солнечно",
    "windy": "Ветрено",
    "windy-variant": "Ветрено",
}


def _weather_icon_svg(condition: str) -> str:
    """Build a compact grayscale SVG without external assets."""
    header = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="180" '
        'viewBox="0 0 240 180">'
        '<rect width="240" height="180" fill="#fff"/>'
    )
    footer = "</svg>"
    sun = (
        '<g stroke="#555" stroke-width="8" stroke-linecap="round">'
        '<line x1="65" y1="10" x2="65" y2="30"/>'
        '<line x1="65" y1="105" x2="65" y2="125"/>'
        '<line x1="10" y1="68" x2="30" y2="68"/>'
        '<line x1="100" y1="68" x2="120" y2="68"/>'
        '<line x1="25" y1="28" x2="38" y2="41"/>'
        '<line x1="92" y1="95" x2="105" y2="108"/>'
        '<line x1="25" y1="108" x2="38" y2="95"/>'
        '<line x1="92" y1="41" x2="105" y2="28"/>'
        '</g><circle cx="65" cy="68" r="29" fill="#aaa" stroke="#333" '
        'stroke-width="6"/>'
    )
    cloud = (
        '<g fill="#bbb" stroke="#333" stroke-width="6">'
        '<circle cx="88" cy="91" r="34"/>'
        '<circle cx="132" cy="72" r="45"/>'
        '<circle cx="174" cy="96" r="34"/>'
        '<rect x="60" y="91" width="145" height="42" rx="20"/>'
        '</g>'
    )
    rain = (
        '<g stroke="#555" stroke-width="7" stroke-linecap="round">'
        '<line x1="88" y1="142" x2="76" y2="166"/>'
        '<line x1="130" y1="142" x2="118" y2="166"/>'
        '<line x1="172" y1="142" x2="160" y2="166"/>'
        '</g>'
    )
    snow = (
        '<g fill="#555">'
        '<circle cx="82" cy="154" r="7"/>'
        '<circle cx="126" cy="163" r="7"/>'
        '<circle cx="174" cy="151" r="7"/>'
        '</g>'
    )

    if condition == "sunny":
        body = sun
    elif condition == "clear-night":
        body = (
            '<circle cx="120" cy="85" r="62" fill="#888"/>'
            '<circle cx="151" cy="61" r="58" fill="#fff"/>'
        )
    elif condition == "partlycloudy":
        body = sun + cloud
    elif condition in ("rainy", "pouring"):
        body = cloud + rain
    elif condition == "lightning-rainy":
        body = (
            cloud
            + rain
            + '<polygon points="135,126 112,157 132,157 116,178 158,142 137,142" '
            'fill="#333"/>'
        )
    elif condition == "lightning":
        body = (
            cloud
            + '<polygon points="133,124 105,162 129,162 112,180 161,140 137,140" '
            'fill="#333"/>'
        )
    elif condition in ("snowy", "hail"):
        body = cloud + snow
    elif condition == "snowy-rainy":
        body = cloud + rain + snow
    elif condition == "fog":
        body = (
            '<g stroke="#777" stroke-width="10" stroke-linecap="round">'
            '<line x1="30" y1="55" x2="205" y2="55"/>'
            '<line x1="55" y1="90" x2="220" y2="90"/>'
            '<line x1="20" y1="125" x2="185" y2="125"/>'
            '</g>'
        )
    elif condition in ("windy", "windy-variant"):
        body = (
            '<g fill="none" stroke="#666" stroke-width="9" stroke-linecap="round">'
            '<path d="M20 58 H165 C205 58 206 20 178 20"/>'
            '<path d="M20 92 H195"/>'
            '<path d="M20 126 H150 C190 126 188 164 160 164"/>'
            '</g>'
        )
    elif condition == "exceptional":
        body = (
            '<circle cx="120" cy="90" r="68" fill="#bbb" stroke="#333" '
            'stroke-width="7"/><rect x="112" y="40" width="16" height="68" '
            'fill="#333"/><circle cx="120" cy="132" r="11" fill="#333"/>'
        )
    else:
        body = cloud

    return header + body + footer


class PanelConfig:
    """Validated panel settings."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.access_key = raw[CONF_ACCESS_KEY]
        self.weather_entity = raw[CONF_WEATHER_ENTITY]
        self.temperature_entity = raw[CONF_TEMPERATURE_ENTITY]
        self.humidity_entity = raw[CONF_HUMIDITY_ENTITY]
        self.lock_entity = raw[CONF_LOCK_ENTITY]
        self.lock_label = raw[CONF_LOCK_LABEL]
        self.icon_cache: dict[str, tuple[float, bytes, str]] = {}

    async def fetch_icon(self, hass: HomeAssistant, url: str) -> tuple[bytes, str] | None:
        """Download and briefly cache a Yandex icon on the HA side."""
        if url.startswith("//"):
            url = "https:" + url

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None

        cached = self.icon_cache.get(url)
        now = time.monotonic()
        if cached is not None and now - cached[0] < ICON_CACHE_SECONDS:
            return cached[1], cached[2]

        try:
            session = async_get_clientsession(hass)
            async with asyncio.timeout(10):
                async with session.get(url) as response:
                    response.raise_for_status()
                    body = await response.content.read(MAX_ICON_BYTES + 1)
                    if len(body) > MAX_ICON_BYTES:
                        raise ValueError("weather icon is too large")
                    content_type = _detected_image_type(body)
                    if content_type is None:
                        raise ValueError("unsupported weather icon format")
        except Exception:  # noqa: BLE001 - fallback SVG is expected on failure
            _LOGGER.debug("Could not proxy weather icon %s", url, exc_info=True)
            return None

        self.icon_cache[url] = (now, body, content_type)
        if len(self.icon_cache) > 32:
            oldest_url = min(self.icon_cache, key=lambda item: self.icon_cache[item][0])
            self.icon_cache.pop(oldest_url, None)
        return body, content_type


class EInkBaseView(HomeAssistantView):
    """Shared panel view helpers."""

    requires_auth = False

    def __init__(self, hass: HomeAssistant, panel_config: PanelConfig) -> None:
        self.hass = hass
        self.panel_config = panel_config

    def authorized(self, request: web.Request) -> bool:
        return _valid_key(request, self.panel_config.access_key)

    @staticmethod
    def forbidden() -> web.Response:
        return web.Response(
            text="Неверный ключ панели",
            status=403,
            content_type="text/plain",
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )


class EInkPanelView(EInkBaseView):
    """Serve the old-browser-compatible panel."""

    url = "/eink-panel"
    name = "api:eink_panel:page"

    async def get(self, request: web.Request) -> web.Response:
        if not self.authorized(request):
            return self.forbidden()

        template_path = Path(__file__).with_name("panel.html")
        template = await self.hass.async_add_executor_job(
            template_path.read_text, "utf-8"
        )
        html = template.replace(
            "__PANEL_KEY_JSON__", json.dumps(self.panel_config.access_key)
        ).replace(
            "__LOCK_LABEL_HTML__", escape(self.panel_config.lock_label)
        ).replace(
            "__LOCK_LABEL_JSON__",
            json.dumps(self.panel_config.lock_label, ensure_ascii=False),
        )

        return web.Response(
            text=html,
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )


class EInkStatusView(EInkBaseView):
    """Return the small JSON payload consumed by the panel."""

    url = "/eink-panel/status"
    name = "api:eink_panel:status"

    async def get(self, request: web.Request) -> web.Response:
        if not self.authorized(request):
            return self.forbidden()

        cfg = self.panel_config
        weather_state = self.hass.states.get(cfg.weather_entity)
        temperature_state = self.hass.states.get(cfg.temperature_entity)
        humidity_state = self.hass.states.get(cfg.humidity_entity)
        lock_state = self.hass.states.get(cfg.lock_entity)

        current_weather: dict[str, str] = {
            "code": "unknown",
            "condition": "Нет данных",
            "temperature": "—",
            "unit": "°C",
            "icon_slot": "current",
            "range": "—",
            "wind": "—",
        }
        if weather_state is not None:
            current_weather = {
                "code": weather_state.state,
                "condition": CONDITION_RU.get(
                    weather_state.state, weather_state.state
                ),
                "temperature": _forecast_number(
                    weather_state.attributes.get("temperature")
                ),
                "unit": str(
                    weather_state.attributes.get("temperature_unit", "°C")
                ),
                "icon_slot": "current",
                "range": "—",
                "wind": (
                    _decimal_number(weather_state.attributes.get("wind_speed"))
                    + " "
                    + str(weather_state.attributes.get("wind_speed_unit", ""))
                    + (
                        " ("
                        + _wind_direction(weather_state.attributes.get("wind_bearing"))
                        + ")"
                        if weather_state.attributes.get("wind_bearing") is not None
                        else ""
                    )
                ).strip(),
            }

        forecast: list[dict[str, str]] = []
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": "hourly"},
                blocking=True,
                target={"entity_id": cfg.weather_entity},
                return_response=True,
            )
            entity_forecast = (response or {}).get(cfg.weather_entity, {})
            raw_forecast = entity_forecast.get("forecast", [])
            current_day = dt_util.now().date()
            day_temperatures = [
                number
                for item in raw_forecast
                if _forecast_local_date(item.get("datetime")) == current_day
                and (number := _as_number(item.get("temperature"))) is not None
            ]
            if day_temperatures:
                current_weather["range"] = (
                    _forecast_number(max(day_temperatures))
                    + "° / "
                    + _forecast_number(min(day_temperatures))
                    + "°"
                )

            previous_day = ""
            for source_index, item in enumerate(raw_forecast[:5]):
                condition_code = str(item.get("condition", ""))
                weekday = _forecast_weekday(item.get("datetime"))
                forecast.append(
                    {
                        "day": weekday if weekday != previous_day else "",
                        "time": _forecast_time(item.get("datetime")),
                        "temperature": _forecast_number(item.get("temperature")) + "°",
                        "code": condition_code or "cloudy",
                        "icon_slot": f"hourly-{source_index}",
                    }
                )
                previous_day = weekday
        except Exception:  # noqa: BLE001 - forecast must not break the panel
            _LOGGER.debug("Could not obtain weather forecast", exc_info=True)

        payload = {
            "weather": current_weather,
            "forecast": forecast,
            "indoor": {
                "temperature": _plain_value(temperature_state),
                "humidity": _plain_value(humidity_state),
            },
            "lock": {
                "state": lock_state.state if lock_state is not None else "unavailable",
                "available": lock_state is not None
                and lock_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE),
                "label": (
                    "Закрыт"
                    if lock_state is not None
                    and lock_state.state in ("locked", "locking")
                    else "Открыт"
                    if lock_state is not None
                    and lock_state.state in ("unlocked", "unlocking", "open")
                    else "Недоступен"
                ),
                "action": (
                    "unlock"
                    if lock_state is not None
                    and lock_state.state in ("locked", "locking")
                    else "lock"
                    if lock_state is not None
                    and lock_state.state in ("unlocked", "unlocking", "open")
                    else "unavailable"
                ),
            },
        }

        return web.json_response(
            payload,
            headers={"Cache-Control": "no-store"},
        )


class EInkWeatherIconView(EInkBaseView):
    """Proxy a Yandex PNG, falling back to a local grayscale SVG."""

    url = "/eink-panel/weather-icon/{slot}"
    name = "api:eink_panel:weather_icon"

    async def get(self, request: web.Request, slot: str) -> web.Response:
        if not self.authorized(request):
            return self.forbidden()

        condition = request.query.get("condition", "cloudy")
        try:
            weather_state = self.hass.states.get(self.panel_config.weather_entity)
            icon_url = ""

            if weather_state is not None:
                if slot == "current":
                    condition = weather_state.state
                    icon_url = str(
                        weather_state.attributes.get("entity_picture", "")
                    )
                elif slot.startswith("hourly-"):
                    index = int(slot.split("-", 1)[1])
                    icons = weather_state.attributes.get("forecast_icons") or (
                        weather_state.attributes.get("forecast_hourly_icons", [])
                    )
                    if isinstance(icons, (list, tuple)) and 0 <= index < len(icons):
                        icon_url = str(icons[index] or "")

            if icon_url:
                proxied = await self.panel_config.fetch_icon(self.hass, icon_url)
                if proxied is not None:
                    body, content_type = proxied
                    return web.Response(
                        body=body,
                        headers={
                            "Content-Type": content_type,
                            "Cache-Control": "private, max-age=300",
                        },
                    )
        except Exception:  # noqa: BLE001 - the endpoint must always show a fallback
            _LOGGER.exception("Unexpected error while serving weather icon")

        return web.Response(
            body=_weather_icon_svg(condition).encode("utf-8"),
            headers={
                "Content-Type": "image/svg+xml; charset=utf-8",
                "Cache-Control": "private, max-age=300",
            },
        )


class EInkLockView(EInkBaseView):
    """Toggle the configured intercom lock according to its current state."""

    url = "/eink-panel/lock"
    name = "api:eink_panel:lock"

    async def post(self, request: web.Request) -> web.Response:
        if not self.authorized(request):
            return self.forbidden()

        lock_entity = self.panel_config.lock_entity
        lock_state = self.hass.states.get(lock_entity)
        if lock_state is None or lock_state.state in (
            STATE_UNKNOWN,
            STATE_UNAVAILABLE,
        ):
            return web.json_response(
                {"ok": False, "message": "Домофон недоступен"}, status=503
            )

        if lock_state.state in ("locked", "locking"):
            service = "unlock"
            message = "Команда открытия отправлена"
        elif lock_state.state in ("unlocked", "unlocking", "open"):
            service = "lock"
            message = "Команда закрытия отправлена"
        else:
            return web.json_response(
                {"ok": False, "message": "Неизвестное состояние домофона"},
                status=409,
            )

        try:
            await self.hass.services.async_call(
                "lock",
                service,
                {},
                blocking=True,
                target={"entity_id": lock_entity},
            )
        except Exception:  # noqa: BLE001 - return a safe UI error
            _LOGGER.exception("Failed to control %s", lock_entity)
            return web.json_response(
                {"ok": False, "message": "Команда не выполнена"}, status=500
            )

        return web.json_response({"ok": True, "message": message})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up E-Ink Panel from configuration.yaml."""
    raw_config = config.get(DOMAIN)
    if raw_config is None:
        return True

    panel_config = PanelConfig(raw_config)
    hass.data[DOMAIN] = panel_config

    hass.http.register_view(EInkPanelView(hass, panel_config))
    hass.http.register_view(EInkStatusView(hass, panel_config))
    hass.http.register_view(EInkWeatherIconView(hass, panel_config))
    hass.http.register_view(EInkLockView(hass, panel_config))

    _LOGGER.info("E-Ink Panel is available at /eink-panel")
    return True
