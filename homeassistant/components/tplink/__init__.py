"""Component to embed TP-Link smart home devices."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from kasa import SmartDevice, SmartDeviceException
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.switch import ATTR_CURRENT_POWER_W, ATTR_TODAY_ENERGY_KWH
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_VOLTAGE, CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .common import SmartDevices, async_discover_devices, get_static_devices
from .const import (
    ATTR_CONFIG,
    ATTR_CURRENT_A,
    ATTR_TOTAL_ENERGY_KWH,
    CONF_DIMMER,
    CONF_DISCOVERY,
    CONF_EMETER_PARAMS,
    CONF_LIGHT,
    CONF_STRIP,
    CONF_SWITCH,
    COORDINATORS,
    PLATFORMS,
    UNAVAILABLE_DEVICES,
)

_LOGGER = logging.getLogger(__name__)

DOMAIN = "tplink"

TPLINK_HOST_SCHEMA = vol.Schema({vol.Required(CONF_HOST): cv.string})

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_LIGHT, default=[]): vol.All(
                    cv.ensure_list, [TPLINK_HOST_SCHEMA]
                ),
                vol.Optional(CONF_SWITCH, default=[]): vol.All(
                    cv.ensure_list, [TPLINK_HOST_SCHEMA]
                ),
                vol.Optional(CONF_STRIP, default=[]): vol.All(
                    cv.ensure_list, [TPLINK_HOST_SCHEMA]
                ),
                vol.Optional(CONF_DIMMER, default=[]): vol.All(
                    cv.ensure_list, [TPLINK_HOST_SCHEMA]
                ),
                vol.Optional(CONF_DISCOVERY, default=True): cv.boolean,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the TP-Link component."""
    conf = config.get(DOMAIN)

    hass.data[DOMAIN] = {}
    hass.data[DOMAIN][ATTR_CONFIG] = conf

    if conf is not None:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_IMPORT}
            )
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up TPLink from a config entry."""
    config_data = hass.data[DOMAIN].get(ATTR_CONFIG)
    if config_data is None and entry.data:
        config_data = entry.data
    elif config_data is not None:
        hass.config_entries.async_update_entry(entry, data=config_data)

    device_registry = dr.async_get(hass)
    tplink_devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    device_count = len(tplink_devices)
    hass_data: dict[str, Any] = hass.data[DOMAIN]

    # These will contain the initialized devices
    hass_data[CONF_LIGHT] = []
    hass_data[CONF_SWITCH] = []
    hass_data[UNAVAILABLE_DEVICES] = []
    lights: list[SmartDevice] = hass_data[CONF_LIGHT]
    switches: list[SmartDevice] = hass_data[CONF_SWITCH]

    # Add static devices
    static_devices = SmartDevices()
    if config_data is not None:
        static_devices = await get_static_devices(config_data)

        lights.extend(static_devices.lights)
        switches.extend(static_devices.switches)

    # Add discovered devices
    if config_data is None or config_data[CONF_DISCOVERY]:
        discovered_devices = await async_discover_devices(
            hass, static_devices, device_count
        )

        lights.extend(discovered_devices.lights)
        switches.extend(discovered_devices.switches)

    if lights:
        _LOGGER.debug(
            "Got %s lights: %s", len(lights), ", ".join(d.host for d in lights)
        )

    if switches:
        _LOGGER.debug(
            "Got %s switches: %s",
            len(switches),
            ", ".join(d.host for d in switches),
        )

    # prepare DataUpdateCoordinators
    coordinators: dict[str, TPLinkDataUpdateCoordinator] = {}
    hass_data[COORDINATORS] = coordinators
    for device in switches + lights:
        coordinator = coordinators[device.device_id] = TPLinkDataUpdateCoordinator(
            hass, device
        )

        try:
            await coordinator.async_refresh()
        except SmartDeviceException:
            _LOGGER.warning(
                "Device at '%s' not reachable during setup, retry not yet implemented",
                device.host,
            )
            continue

    hass.config_entries.async_setup_platforms(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    hass_data: dict[str, Any] = hass.data[DOMAIN]
    if unload_ok:
        hass_data.clear()

    return unload_ok


class TPLinkDataUpdateCoordinator(DataUpdateCoordinator):
    """DataUpdateCoordinator to gather data for specific SmartPlug."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: SmartDevice,
    ) -> None:
        """Initialize DataUpdateCoordinator to gather data for specific SmartPlug."""
        self.device = device
        self.update_children = True
        update_interval = timedelta(seconds=10)
        super().__init__(
            hass,
            _LOGGER,
            name=device.alias,
            update_interval=update_interval,
        )

    async def _async_update_data(self) -> dict:
        """Fetch all device and sensor data from api."""
        try:
            await self.device.update(update_children=self.update_children)
        except SmartDeviceException as ex:
            raise UpdateFailed(ex) from ex
        else:
            self.update_children = True

        self.name = self.device.alias

        # Check if the device has emeter
        if not self.device.has_emeter:
            return {}

        if (emeter_today := self.device.emeter_today) is not None:
            consumption_today = emeter_today
        else:
            # today's consumption not available, when device was off all the day
            # bulb's do not report this information, so filter it out
            consumption_today = None if self.device.is_bulb else 0.0

        emeter_readings = self.device.emeter_realtime
        return {
            CONF_EMETER_PARAMS: {
                ATTR_CURRENT_POWER_W: emeter_readings.power,
                ATTR_TOTAL_ENERGY_KWH: emeter_readings.total,
                ATTR_VOLTAGE: emeter_readings.voltage,
                ATTR_CURRENT_A: emeter_readings.current,
                ATTR_TODAY_ENERGY_KWH: consumption_today,
            }
        }
