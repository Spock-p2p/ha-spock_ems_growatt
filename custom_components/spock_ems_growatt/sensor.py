import logging
import math
import voluptuous as vol
from datetime import timedelta

from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import (
    CONF_NAME, CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL,
    POWER_WATT, ENERGY_KILO_WATT_HOUR,
    DEVICE_CLASS_POWER, DEVICE_CLASS_ENERGY, DEVICE_CLASS_BATTERY,
    PERCENTAGE
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity
from homeassistant.util import Throttle

# Importar pymodbus dependiendo de la versión instalada en HA
try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    from pymodbus.client.sync import ModbusTcpClient

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = 'Growatt EMS'
DEFAULT_SCAN_INTERVAL = timedelta(seconds=10)
DEFAULT_PORT = 502

# Definimos las claves de configuración
CONF_MODBUS_ID = 'modbus_id'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_HOST): cv.string,
    vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
    vol.Optional(CONF_MODBUS_ID, default=1): cv.positive_int,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): cv.time_period,
})

# Mapeo de Sensores
# Formato: [Register Address, Unit, Icon, Device Class]
# NOTA: La lógica del Grid la sobrescribiremos en el código, el registro aquí es placeholder.
SENSOR_TYPES = {
    'solar_power': [3001, POWER_WATT, 'mdi:solar-power', DEVICE_CLASS_POWER],
    'grid_power':  [3048, POWER_WATT, 'mdi:transmission-tower', DEVICE_CLASS_POWER], # Usaremos lógica custom
    'load_power':  [0,    POWER_WATT, 'mdi:home-lightning-bolt', DEVICE_CLASS_POWER], # Calculado
    'bat_power':   [0,    POWER_WATT, 'mdi:battery-charging', DEVICE_CLASS_POWER],    # Calculado
    'bat_soc':     [3010, PERCENTAGE, 'mdi:battery-50', DEVICE_CLASS_BATTERY],
}

def setup_platform(hass, config, add_entities, discovery_info=None):
    """Setup the Growatt EMS platform."""
    host = config.get(CONF_HOST)
    port = config.get(CONF_PORT)
    modbus_id = config.get(CONF_MODBUS_ID)
    name = config.get(CONF_NAME)
    scan_interval = config.get(CONF_SCAN_INTERVAL)

    # Inicializamos el Hub de datos
    hub = GrowattHub(host, port, modbus_id)
    hub.update()

    sensors = []
    for sensor_key, params in SENSOR_TYPES.items():
        sensors.append(GrowattEmsSensor(hub, name, sensor_key, params[1], params[2], params[3]))

    add_entities(sensors, True)


class GrowattHub:
    """Clase para manejar la conexión Modbus y leer datos en bloque."""

    def __init__(self, host, port, modbus_id):
        self._client = ModbusTcpClient(host=host, port=port)
        self._modbus_id = modbus_id
        self._data = [0] * 100 # Buffer inicial
        self._bat_data = [0] * 50

    def get_data(self):
        return self._data
    
    def get_bat_data(self):
        return self._bat_data

    @Throttle(timedelta(seconds=5))
    def update(self):
        """Lee los registros del inversor."""
        if not self._client.connect():
            _LOGGER.error("No se pudo conectar al Inversor Growatt")
            return

        try:
            # 1. LEER BLOQUE PRINCIPAL (Solar + Grid)
            # Leemos desde el 3000 hasta el 3055 (55 registros) para asegurar que pillamos el 3048
            # Tu script usa Input Registers (FC04)
            req = self._client.read_input_registers(3000, 55, device_id=self._modbus_id) # Pymodbus v3 usa slave/device_id
            
            # Compatibilidad con Pymodbus v2 (unit) si falla
            if getattr(req, 'isError', lambda: True)(): 
                 req = self._client.read_input_registers(3000, 55, unit=self._modbus_id)

            if not req.isError():
                self._data = req.registers
            else:
                _LOGGER.error("Error leyendo registros principales (3000-3055)")

            # 2. LEER BLOQUE BATERÍA (Alrededor del 3170)
            # Tu script usa 3178 y 3180. Leemos un bloque desde 3170.
            req_bat = self._client.read_input_registers(3170, 20, device_id=self._modbus_id)
            if getattr(req_bat, 'isError', lambda: True)():
                 req_bat = self._client.read_input_registers(3170, 20, unit=self._modbus_id)

            if not req_bat.isError():
                self._bat_data = req_bat.registers
            
        except Exception as e:
            _LOGGER.error("Excepción leyendo Modbus: %s", e)
        finally:
            self._client.close()


class GrowattEmsSensor(Entity):
    """Representación de un sensor Growatt."""

    def __init__(self, hub, name, sensor_key, unit, icon, device_class):
        self._hub = hub
        self._key = sensor_key
        self._name = f"{name} {sensor_key.replace('_', ' ').title()}"
        self._unit = unit
        self._icon = icon
        self._device_class = device_class
        self._state = None

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

    def decode_u32(self, data, index):
        """Decodifica 2 registros como Unsigned 32-bit (Big Endian)."""
        if index + 1 < len(data):
            return (data[index] << 16) | data[index + 1]
        return 0

    def decode_s16(self, val):
        """Decodifica 1 registro como Signed 16-bit."""
        if val is not None and (val & 0x8000):
            return val - 0x10000
        return val

    def update(self):
        """Calcula el estado del sensor basándose en los datos crudos."""
        self._hub.update()
        data = self._hub.get_data()      # Bloque 3000 (Indice 0 = Reg 3000)
        bat_data = self._hub.get_bat_data() # Bloque 3170 (Indice 0 = Reg 3170)

        # Si no hay datos, no actualizamos
        if not data or len(data) < 50:
            return

        # --- VALORES BASICOS ---
        # Solar (3001-3002) -> Indices 1 y 2
        p_solar = self.decode_u32(data, 1) * 0.1

        # Batería (Carga 3180 / Descarga 3178)
        # 3180 es índice 10 en bat_data (3170+10)
        # 3178 es índice 8 en bat_data
        p_bat_charge = 0
        p_bat_discharge = 0
        if bat_data and len(bat_data) > 10:
            p_bat_charge = self.decode_u32(bat_data, 10) * 0.1
            p_bat_discharge = self.decode_u32(bat_data, 8) * 0.1
        
        p_bat_net = p_bat_charge - p_bat_discharge # (+) Cargando, (-) Descargando

        # --- GRID (LA PARTE CRITICA) ---
        # Registro 3048 -> Indice 48 en data
        # Tu script: grid_raw * -1 * math.sqrt(3)
        try:
            raw_3048 = data[48]
            val_3048 = self.decode_s16(raw_3048) * 0.1
            p_grid = val_3048 * -1 * math.sqrt(3)
        except (IndexError, TypeError):
            p_grid = 0

        # --- LOAD (Calculado) ---
        # Load = Grid + Solar - Bat_Net
        p_load = p_grid + p_solar - p_bat_net

        # --- ASIGNACIÓN DE ESTADOS ---
        if self._key == 'solar_power':
            self._state = round(p_solar, 1)
        
        elif self._key == 'grid_power':
            self._state = round(p_grid, 1)
        
        elif self._key == 'load_power':
            # Load no puede ser negativo físicamente (errores de redondeo)
            self._state = round(max(0, p_load), 1)
        
        elif self._key == 'bat_power':
            self._state = round(p_bat_net, 1)
        
        elif self._key == 'bat_soc':
            # SOC está en 3010 (Indice 10 en data)
            # O en 3171 (Indice 1 en bat_data) si el otro es 0
            soc = data[10]
            if soc == 0 and bat_data and len(bat_data) > 1:
                soc = bat_data[1]
            self._state = soc
