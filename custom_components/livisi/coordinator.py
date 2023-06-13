"""Code to manage fetching LIVISI data API."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from aiohttp import ClientConnectorError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aiolivisi.aiolivisi import AioLivisi
from .aiolivisi.livisi_event import LivisiEvent
from .aiolivisi.websocket import Websocket
from .aiolivisi.const import (
    EVENT_BUTTON_PRESSED as LIVISI_EVENT_BUTTON_PRESSED,
    EVENT_MOTION_DETECTED as LIVISI_EVENT_MOTION_DETECTED,
    EVENT_STATE_CHANGED as LIVISI_EVENT_STATE_CHANGED,
)
from .aiolivisi.errors import TokenExpiredException

from .const import (
    AVATAR,
    AVATAR_PORT,
    CLASSIC_PORT,
    CONF_HOST,
    CONF_PASSWORD,
    DEVICE_POLLING_DELAY,
    EVENT_BUTTON_PRESSED,
    EVENT_MOTION_DETECTED,
    LIVISI_REACHABILITY_CHANGE,
    LIVISI_STATE_CHANGE,
    LOGGER,
)


class LivisiDataUpdateCoordinator(DataUpdateCoordinator[list[dict[str, Any]]]):
    """Class to manage fetching LIVISI data API."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, config_entry: ConfigEntry, aiolivisi: AioLivisi
    ) -> None:
        """Initialize my coordinator."""
        super().__init__(
            hass,
            LOGGER,
            name="Livisi devices",
            update_interval=timedelta(seconds=DEVICE_POLLING_DELAY),
        )
        self.config_entry = config_entry
        self.hass = hass
        self.aiolivisi = aiolivisi
        self.websocket = Websocket(aiolivisi)
        self.devices: set[str] = set()
        self.capability_to_device: dict[str, str] = {}
        self.rooms: dict[str, Any] = {}
        self.serial_number: str = ""
        self.controller_type: str = ""
        self.is_avatar: bool = False
        self.port: int = 0

    async def _async_update_data(self) -> list[dict[str, Any]]:
        """Get device configuration from LIVISI."""
        try:
            return await self.async_get_devices()
        except TokenExpiredException:
            await self.aiolivisi.async_set_token(self.aiolivisi.livisi_connection_data)
            return await self.async_get_devices()
        except ClientConnectorError as exc:
            raise UpdateFailed("Failed to get livisi devices from controller") from exc

    def _async_dispatcher_send(self, event: str, source: str, data: Any) -> None:
        if data is not None:
            async_dispatcher_send(self.hass, f"{event}_{source}", data)

    async def async_setup(self) -> None:
        """Set up the Livisi Smart Home Controller."""
        if not self.aiolivisi.livisi_connection_data:
            livisi_connection_data = {
                "ip_address": self.config_entry.data[CONF_HOST],
                "password": self.config_entry.data[CONF_PASSWORD],
            }

            await self.aiolivisi.async_set_token(
                livisi_connection_data=livisi_connection_data
            )
        controller_data = await self.aiolivisi.async_get_controller()
        if (controller_type := controller_data["controllerType"]) == AVATAR:
            self.port = AVATAR_PORT
            self.is_avatar = True
        else:
            self.port = CLASSIC_PORT
            self.is_avatar = False
        self.controller_type = controller_type
        self.serial_number = controller_data["serialNumber"]

    async def async_get_devices(self) -> list[dict[str, Any]]:
        """Set the discovered devices list."""
        devices = await self.aiolivisi.async_get_devices()
        capability_mapping = {}
        for device in devices:
            for capability_id in device.get("capabilities", []):
                capability_mapping[capability_id] = device["id"]
        self.capability_to_device = capability_mapping
        return devices

    async def async_get_device_state(self, capability: str, key: str) -> Any | None:
        """Get state from livisi devices."""
        response: dict[str, Any] = await self.aiolivisi.async_get_device_state(
            capability[1:]
        )
        if response is None:
            return None
        return response.get(key, {}).get("value")

    async def async_set_all_rooms(self) -> None:
        """Set the room list."""
        response: list[dict[str, Any]] = await self.aiolivisi.async_get_all_rooms()

        for available_room in response:
            available_room_config: dict[str, Any] = available_room["config"]
            self.rooms[available_room["id"]] = available_room_config["name"]

    def get_room_name(self, device: dict[str, Any]) -> str | None:
        """Get the room name from a device."""
        room_id: str | None = device.get("location")
        room_name: str | None = None
        if room_id is not None:
            room_name = self.rooms.get(room_id)
        return room_name

    def on_data(self, event_data: LivisiEvent) -> None:
        """Define a handler to fire when the data is received."""
        if event_data.type == LIVISI_EVENT_BUTTON_PRESSED:
            device_id = self.capability_to_device.get(event_data.source)
            if device_id is not None:
                livisi_event_data = {
                    "device_id": device_id,
                    "type": EVENT_BUTTON_PRESSED,
                    "button_index": event_data.properties.get("index", 0),
                    "press_type": event_data.properties.get("type", "ShortPress"),
                }
                self.hass.bus.async_fire("livisi_event", livisi_event_data)
        elif event_data.type == LIVISI_EVENT_MOTION_DETECTED:
            device_id = self.capability_to_device.get(event_data.source)
            if device_id is not None:
                livisi_event_data = {
                    "device_id": device_id,
                    "type": EVENT_MOTION_DETECTED,
                }
                self.hass.bus.async_fire("livisi_event", livisi_event_data)
        elif event_data.type == LIVISI_EVENT_STATE_CHANGED:
            self._async_dispatcher_send(
                LIVISI_STATE_CHANGE, event_data.source, event_data.onState
            )
            self._async_dispatcher_send(
                LIVISI_STATE_CHANGE, event_data.source, event_data.vrccData
            )
            self._async_dispatcher_send(
                LIVISI_STATE_CHANGE, event_data.source, event_data.isOpen
            )
            self._async_dispatcher_send(
                LIVISI_STATE_CHANGE, event_data.source, event_data.luminance
            )
            self._async_dispatcher_send(
                LIVISI_REACHABILITY_CHANGE, event_data.source, event_data.isReachable
            )

    async def on_close(self) -> None:
        """Define a handler to fire when the websocket is closed."""
        for device_id in self.devices:
            self._async_dispatcher_send(LIVISI_REACHABILITY_CHANGE, device_id, False)

        await self.websocket.connect(self.on_data, self.on_close, self.port)

    async def ws_connect(self) -> None:
        """Connect the websocket."""
        await self.websocket.connect(self.on_data, self.on_close, self.port)
