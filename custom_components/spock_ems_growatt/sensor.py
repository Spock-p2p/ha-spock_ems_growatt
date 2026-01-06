import logging
import math
import voluptuous as vol
from datetime import timedelta

from homeassistant.components.sensor import (
    PLATFORM_SCHEMA,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    CONF_NAME, CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL,
    PERCENTAGE,
    UnitOfPower,
    UnitOfEnergy
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity
from homeassistant.util import Throttle

# Importar pymodbus dependiendo de la versión instalada en HA
try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    try:
        from pymodbus.client.sync import ModbusTcpClient
    except ImportError:
        # Fallback para versiones muy nuevas de HA que usan estructura diferente
        from pymodbus.client import ModbusTcpClient

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

# --- MAPEO DE SENSORES ---
# Hemos eliminado las constantes antiguas (POWER_WATT, DEVICE_CLASS_POWER)
# y usamos cadenas de texto o las nuevas clases UnitOfPower.
# Formato: [Register Address, Unit, Icon, Device Class]

# Nota: Para máxima compatibilidad usamos strings directos: "W", "power", "battery"
SENSOR_TYPES = {
    'solar_power': [3001, "W", 'mdi:solar-power', "power"],
    'grid_power':  [3048, "W", 'mdi:transmission-tower', "power"], # Lógica custom
    'load_power':  [0,    "W", 'mdi:home-lightning-bolt', "power"], # Calculado
    'bat_power':   [0,    "W", 'mdi:battery-charging', "power"],    # Calculado
    'bat_soc':     [3010, PERCENTAGE, 'mdi:battery-50', "battery"],
}

def setup_platform(hass, config, add_entities, discovery_info=None):
    """Setup the Growatt EMS platform."""
    host = config.get(CONF_HOST)
    port = config.get(CONF_PORT)
    modbus_id = config.get(CONF_MODBUS_ID)
    name = config.get(CONF_NAME)
    
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
        # Intentamos instanciar el cliente de forma compatible
        self._client = ModbusTcpClient(host=host, port=port)
        self._modbus_id = modbus_id
        self._data = [0] * 100 
        self._bat_data = [0] * 50

    def get_data(self):
        return self._data
    
    def get_bat_data(self):
        return self._bat_data

    @Throttle(timedelta(seconds=5))
    def update(self):
        """Lee los registros del inversor."""
        try:
            if not self._client.connect():
                _LOGGER.error("No se pudo conectar al Inversor Growatt (Modbus TCP) en %s", self._client.host if hasattr(self._client, 'host') else 'IP desconocida')
                return
        except Exception as e:
            _LOGGER.error("Error conectando modbus: %s", e)
            return

        try:
            # 1. LEER BLOQUE PRINCIPAL (Solar + Grid)
            # Leemos desde el 3000 hasta el 3055 (55 registros) para asegurar que pillamos el 3048
            
            # Pymodbus v3+ usa 'slave', v2 usa 'unit'
            kwargs = {'slave': self._modbus_id}
            try:
                # Intentamos llamada estilo v3
                req = self._client.read_input_registers(3000, 55, **kwargs)
            except TypeError:
                # Fallback a estilo v2
                kwargs = {'unit': self._modbus_id}
                req = self._client.read_input_registers(3000, 55, **kwargs)
            
            # Verificación de error genérica
            if getattr(req, 'isError', lambda: True)(): 
                 # Último intento desesperado sin argumentos extra
                 try:
                    req = self._client.read_input_registers(3000, 55)
                 except:
                    pass

            if req and not getattr(req, 'isError', lambda: True)():
                self._data = req.registers
            else:
                _LOGGER.debug("Error leyendo registros principales (3000-3055)")

            # 2. LEER BLOQUE BATERÍA (Alrededor del 3170)
            try:
                # Intentamos estilo v3
                req_bat = self._client.read_input_registers(3170, 20, slave=self._modbus_id)
            except TypeError:
                # Fallback estilo v2
                req_bat = self._client.read_input_registers(3170, 20, unit=self._modbus_id)

            if req_bat and not getattr(req_bat, 'isError', lambda: True)():
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
        # Formatear nombre amigable
        friendly_key = sensor_key.replace('_', ' ').title()
        self._name = f"{name} {friendly_key}"
        self._unit = unit
        self._icon = icon
        self._device_class = device_class
        self._state = None
        self._attr_state_class = SensorStateClass.MEASUREMENT

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
        data = self._hub.get_data()      # Bloque 3000
        bat_data = self._hub.get_bat_data() # Bloque 3170

        # Si no hay datos suficientes, no actualizamos
        if not data or len(data) < 50:
            return

        # --- VALORES BASICOS ---
        # Solar (3001-3002) -> Indices
