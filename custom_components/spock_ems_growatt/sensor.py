"""Plataforma de sensores para Spock EMS Growatt."""
from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower, PERCENTAGE
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, CONF_INVERTER_IP

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    
    # Definimos los sensores con todas las etiquetas necesarias para el Panel de Energía
    entities = [
        GrowattSpockSensor(
            coordinator, 
            name="PV Power", 
            key="pv_power", 
            unit=UnitOfPower.WATT, 
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT
        ),
        GrowattSpockSensor(
            coordinator, 
            name="Grid Power", 
            key="net_grid_power", 
            unit=UnitOfPower.WATT, 
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT
        ),
        GrowattSpockSensor(
            coordinator, 
            name="Battery SOC", 
            key="battery_soc_total", 
            unit=PERCENTAGE, 
            device_class=SensorDeviceClass.BATTERY,
            state_class=SensorStateClass.MEASUREMENT
        ),
        GrowattSpockSensor(
            coordinator, 
            name="Battery Power", 
            key="battery_power", 
            unit=UnitOfPower.WATT, 
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT
        ),
    ]
    async_add_entities(entities)

class GrowattSpockSensor(SensorEntity):
    def __init__(self, coordinator, name, key, unit, device_class, state_class):
        self.coordinator = coordinator
        self._name = name
        self._key = key
        self._unit = unit
        self._device_class = device_class
        self._state_class = state_class
        # Usamos la IP como identificador único del dispositivo físico
        self._ip = coordinator.entry_data[CONF_INVERTER_IP]

    @property
    def unique_id(self):
        """ID único interno para la entidad."""
        return f"growatt_{self._ip}_{self._key}"

    @property
    def name(self):
        """Nombre legible del sensor."""
        return f"Growatt Spock {self._name}"

    @property
    def state(self):
        """Valor del sensor."""
        return self.coordinator.data.get(self._key)

    @property
    def unit_of_measurement(self):
        """Unidad de medida (W, %, etc)."""
        return self._unit
        
    @property
    def device_class(self):
        """Define el tipo de dato (Power, Battery, etc)."""
        return self._device_class

    @property
    def state_class(self):
        """Define cómo se comporta el dato (Measurement para valores instantáneos)."""
        return self._state_class

    @property
    def should_poll(self):
        """No hacemos poll individual, el coordinador se encarga."""
        return False

    @property
    def device_info(self) -> DeviceInfo:
        """Información para agrupar las entidades en un 'Dispositivo'."""
        return {
            "identifiers": {(DOMAIN, self._ip)},
            "name": f"Growatt Inverter ({self._ip})",
            "manufacturer": "Growatt",
            "model": "Spock EMS Controller",
            "sw_version": "1.0.0",
            "configuration_url": f"http://{self._ip}",
        }

    async def async_added_to_hass(self):
        """Cuando se añade a HA, nos suscribimos al coordinador."""
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))
