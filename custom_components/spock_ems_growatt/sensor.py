"""Plataforma de sensores para Spock EMS Growatt."""
from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower, PERCENTAGE
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_INVERTER_IP


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities = [
        GrowattSpockSensor(
            coordinator,
            name="PV Power",
            key="pv_power",
            unit=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        GrowattSpockSensor(
            coordinator,
            name="Grid Power",
            key="net_grid_power",
            unit=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        GrowattSpockSensor(
            coordinator,
            name="Load Power",
            key="supply_power",
            unit=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        GrowattSpockSensor(
            coordinator,
            name="Battery SOC",
            key="battery_soc_total",
            unit=PERCENTAGE,
            device_class=SensorDeviceClass.BATTERY,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        GrowattSpockSensor(
            coordinator,
            name="Battery Power",
            key="battery_power",
            unit=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
        ),
    ]

    async_add_entities(entities)


class GrowattSpockSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, name, key, unit, device_class, state_class):
        super().__init__(coordinator)
        self._name = name
        self._key = key
        self._ip = coordinator.entry_data[CONF_INVERTER_IP]

        # Atributos “modernos” de HA
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class

        # Guardamos último valor válido para evitar "unknown" por ciclos malos
        self._attr_native_value = None

    @property
    def unique_id(self):
        return f"growatt_{self._ip}_{self._key}"

    @property
    def name(self):
        return f"Growatt Spock {self._name}"

    @property
    def device_info(self) -> DeviceInfo:
        return {
            "identifiers": {(DOMAIN, self._ip)},
            "name": f"Growatt Inverter ({self._ip})",
            "manufacturer": "Growatt",
            "model": "Spock EMS Controller",
            "sw_version": "1.0.0",
            "configuration_url": f"http://{self._ip}",
        }

    def _handle_coordinator_update(self) -> None:
        """Actualización desde el coordinador.
        Solo actualiza si viene dato (evita pasar a unknown si falta un ciclo).
        """
        data = self.coordinator.data
        if isinstance(data, dict):
            v = data.get(self._key)
            if v is not None:
                self._attr_native_value = v

        self.async_write_ha_state()
