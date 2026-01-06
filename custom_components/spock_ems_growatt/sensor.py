import logging
import math
import voluptuous as vol
from datetime import timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
    PLATFORM_SCHEMA,
)
from homeassistant.const import (
    CONF_NAME, CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL,
    PERCENTAGE,
    UnitOfPower,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity
from homeassistant.util import Throttle

# Importar pymodbus con fallback
try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    try:
        from pymodbus.client.sync import ModbusTcpClient
    except ImportError:
        from pymodbus.client import ModbusTcpClient

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = 'Growatt EMS'
DEFAULT_SCAN_INTERVAL = timedelta(seconds=10)
DEFAULT_PORT = 502

CONF_MODBUS_ID = 'modbus_id'

# Esquema para validación (aunque se use Config Entry, se suele mantener)
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_HOST): cv.string,
    vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
    vol.Optional(CONF_MODBUS_ID, default=1): cv.positive_int,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): cv.time_period,
})

# --- MAPEO DE SENSORES ---
# [Register Address, Unit, Icon, Device Class]
SENSOR_TYPES = {
    'solar_power': [3001, "W", 'mdi:solar-power', "power"],
    'grid_power':  [3048, "W", 'mdi:transmission-tower', "power"],
    'load_power':  [0,    "W", 'mdi:home-lightning-bolt', "power"],
    'bat_power':   [0,    "W", 'mdi:battery-charging', "power"],
    'bat_soc':     [3010, PERCENTAGE, 'mdi:battery-50', "battery"],
}

async def async_setup_entry(hass, config_entry, async_add_entities):
    """Configuración moderna mediante Config Entry (UI)."""
    config = config_entry.data

    host = config.get(CONF_HOST)
    port = config.get(CONF_PORT, DEFAULT_PORT)
    modbus_id = config.get(CONF_MODBUS_ID, 1)
    name = config.get(CONF_NAME, DEFAULT_NAME)
    
    # Inicializamos el Hub
    hub = GrowattHub(host, port, modbus_id)

    # Creamos las entidades
    sensors = []
    for sensor_key, params in SENSOR_TYPES.items():
        sensors.append(GrowattEmsSensor(hub, name, sensor_key, params[1], params[2], params[3]))

    # Añadimos las entidades a HA
    async_add_entities(sensors, update_before_add=True)


class GrowattHub:
    """Clase para manejar la conexión Modbus y leer datos en bloque."""

    def __init__(self, host, port, modbus_id):
        self._host = host
        self._port = port
        self._modbus_id = modbus_id
        # Cliente se instancia en cada update o aquí, dependiendo de la librería
        self._client = ModbusTcpClient(host=host, port=port)
        self._data = [0] * 100 
        self._bat_data = [0] * 50

    def get_data(self):
        return self._data
    
    def get_bat_data(self):
        return self._bat_data

    @Throttle(timedelta(seconds=5))
    def update(self):
        """Lee los registros del inversor (Ejecutado en hilo Sync por HA)."""
        # Intentar reconectar si es necesario
        try:
            self._client.connect()
        except Exception as e:
            _LOGGER.error("Error conectando a %s: %s", self._host, e)
            return

        try:
            # 1. LEER BLOQUE PRINCIPAL (Solar + Grid)
            # Intentamos leer 55 registros empezando en 3000
            kwargs = {'slave': self._modbus_id}
            try:
                req = self._client.read_input_registers(3000, 55, **kwargs)
            except TypeError:
                kwargs = {'unit': self._modbus_id}
                req = self._client.read_input_registers(3000, 55, **kwargs)
            
            # Verificación de error
            if getattr(req, 'isError', lambda: True)(): 
                 # Fallback sin argumentos extra
                 try:
                    req = self._client.read_input_registers(3000, 55)
                 except:
                    pass

            if req and not getattr(req, 'isError', lambda: True)():
                self._data = req.registers
            else:
                _LOGGER.debug("Fallo lectura bloque principal (3000-3055)")

            # 2. LEER BLOQUE BATERÍA (3170)
            try:
                req_bat = self._client.read_input_registers(3170, 20, slave=self._modbus_id)
            except TypeError:
                req_bat = self._client.read_input_registers(3170, 20, unit=self._modbus_id)

            if req_bat and not getattr(req_bat, 'isError', lambda: True)():
                self._bat_data = req_bat.registers
            
        except Exception as e:
            _LOGGER.error("Excepción Modbus: %s", e)
        finally:
            self._client.close()


class GrowattEmsSensor(Entity):
    """Sensor Growatt."""

    def __init__(self, hub, name, sensor_key, unit, icon, device_class):
        self._hub = hub
        self._key = sensor_key
        self._name = f"{name} {sensor_key.replace('_', ' ').title()}"
        self._unit = unit
        self._icon = icon
        self._device_class = device_class
        self._state = None
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_unique_id = f"{name}_{sensor_key}".lower().replace(" ", "_")

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return self._state

    @property
    def unit_of_measurement(self):
        return self._unit

    @property
    def icon(self):
        return self._icon

    @property
    def device_class(self):
        return self._device_class
    
    @property
    def unique_id(self):
        return self._attr_unique_id

    def decode_u32(self, data, index):
        if index + 1 < len(data):
            return (data[index] << 16) | data[index + 1]
        return 0

    def decode_s16(self, val):
        if val is not None and (val & 0x8000):
            return val - 0x10000
        return val

    def update(self):
        """Calcula el estado (HA llama a esto automáticamente)."""
        self._hub.update()
        data = self._hub.get_data()
        bat_data = self._hub.get_bat_data()

        if not data or len(data) < 50:
            return

        # --- LOGICA MATEMÁTICA ---
        # Solar (3001-3002)
        p_solar = self.decode_u32(data, 1) * 0.1

        # Batería
        p_bat_charge = 0
        p_bat_discharge = 0
        if bat_data and len(bat_data) > 10:
            p_bat_charge = self.decode_u32(bat_data, 10) * 0.1
            p_bat_discharge = self.decode_u32(bat_data, 8) * 0.1
        
        p_bat_net = p_bat_charge - p_bat_discharge

        # GRID (3048)
        try:
            raw_3048 = data[48]
            val_3048 = self.decode_s16(raw_3048) * 0.1
            p_grid = val_3048 * -1 * math.sqrt(3)
        except (IndexError, TypeError):
            p_grid = 0

        # LOAD
        p_load = p_grid + p_solar - p_bat_net

        # Asignar estado
        if self._key == 'solar_power':
            self._state = round(p_solar, 1)
        elif self._key == 'grid_power':
            self._state = round(p_grid, 1)
        elif self._key == 'load_power':
            self._state = round(max(0, p_load), 1)
        elif self._key == 'bat_power':
            self._state = round(p_bat_net, 1)
        elif self._key == 'bat_soc':
            soc = data[10]
            if soc == 0 and bat_data and len(bat_data) > 1:
                soc = bat_data[1]
            self._state = soc
