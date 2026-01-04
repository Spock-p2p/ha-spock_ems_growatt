from homeassistant.components.sensor import SensorEntity
from .const import DOMAIN

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    
    entities = [
        GrowattSpockSensor(coordinator, "PV Power", "pv_power", "W"),
        GrowattSpockSensor(coordinator, "Grid Power", "net_grid_power", "W"),
        GrowattSpockSensor(coordinator, "Battery SOC", "battery_soc_total", "%"),
        GrowattSpockSensor(coordinator, "Battery Power", "battery_power", "W"),
    ]
    async_add_entities(entities)

class GrowattSpockSensor(SensorEntity):
    def __init__(self, coordinator, name, key, unit):
        self.coordinator = coordinator
        self._name = name
        self._key = key
        self._unit = unit

    @property
    def name(self): return f"Growatt Spock {self._name}"

    @property
    def state(self): return self.coordinator.data.get(self._key)

    @property
    def unit_of_measurement(self): return self._unit

    @property
    def should_poll(self): return False

    async def async_added_to_hass(self):
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))
