"""Plataforma de sensores para Spock EMS Growatt."""
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, CONF_INVERTER_IP

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    
    # Definimos los sensores con sus unidades y clases de dispositivo si fuera necesario
    # Para que quede bonito, podríamos añadir device_class en el futuro
    entities = [
        GrowattSpockSensor(coordinator, "PV Power", "pv_power", "W", "power"),
        GrowattSpockSensor(coordinator, "Grid Power", "net_grid_power", "W", "power"),
        GrowattSpockSensor(coordinator, "Battery SOC", "battery_soc_total", "%", "battery"),
        GrowattSpockSensor(coordinator, "Battery Power", "battery_power", "W", "power"),
    ]
    async_add_entities(entities)

class GrowattSpockSensor(SensorEntity):
    def __init__(self, coordinator, name, key, unit, device_class=None):
        self.coordinator = coordinator
        self._name = name
        self._key = key
        self._unit = unit
        self._device_class = device_class
        # Usamos la IP como identificador único del dispositivo físico
        self._ip = coordinator.entry_data[CONF_INVERTER_IP]

    @property
    def unique_id(self):
        """ID único interno para la entidad (sensor)."""
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
        """Define el tipo de dato (para que HA ponga el icono correcto: rayo, batería, etc)."""
        return self._device_class

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
            "configuration_url": f"http://{self._ip}", # Enlace clicable a la IP del inversor
        }

    async def async_added_to_hass(self):
        """Cuando se añade a HA, nos suscribimos al coordinador."""
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))
