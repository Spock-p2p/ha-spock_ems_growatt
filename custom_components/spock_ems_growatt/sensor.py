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
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity
from homeassistant.util import Throttle

# Importamos las constantes EXACTAS de tu componente
from .const import (
    DEFAULT_PORT,
    DEFAULT_NAME,
    CONF_MODBUS_ID,
    DEFAULT_MODBUS_ID,
)

# Importación de Pymodbus
try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    try:
        from pymodbus.client.sync import ModbusTcpClient
    except ImportError:
        from pymodbus.client import ModbusTcpClient

_LOGGER = logging.getLogger(__name__)

DEFAULT_SCAN_INTERVAL = timedelta(seconds=10)

# Mapeo de Sensores
SENSOR_TYPES = {
    'solar_power': [3001, "W", 'mdi:solar-power', "power"],
    'grid_power':  [3048, "W", 'mdi:transmission-tower', "power"],
    'load_power':  [0,    "W", 'mdi:home-lightning-bolt', "power"],
    'bat_power':   [0,    "W", 'mdi:battery-charging', "power"],
    'bat_soc':     [3010, PERCENTAGE, 'mdi:battery-50', "battery"],
}

async def async_setup_entry(hass, config_entry, async_add_entities):
    """Configuración usando Config Entry (lo que usa tu HA)."""
    config = config_entry.data
    
    # 1. OBTENCIÓN DE DATOS (Sin inventar, buscando en las claves estándar)
    host = config.get(CONF_HOST) or config.get("host") or config.get("ip_address")
    port = config.get(CONF_PORT) or config.get("port") or DEFAULT_PORT
    modbus_id = config.get(CONF_MODBUS_ID) or config.get("modbus_id") or DEFAULT_MODBUS_ID
    name = config.get(CONF_NAME) or config.get("name") or DEFAULT_NAME
    
    if not host:
        _LOGGER.error("Growatt EMS: No se encontró HOST/IP en la configuración.")
        return

    # Inicializamos el Hub
    hub = GrowattHub(host, port, modbus_id)
    
    sensors = []
    for sensor_key, params in SENSOR_TYPES.items():
        sensors.append(GrowattEmsSensor(hub, name, sensor_key, params[1], params[2], params[3]))

    async_add_entities(sensors, update_before_add=True)


class GrowattHub:
    def __init__(self, host, port, modbus_id):
        self._host = host
        self._port = port
        self._modbus_id = modbus_id
        # Cliente Modbus TCP estándar
        self._client = ModbusTcpClient(host=host, port=port)
        self._data = [0] * 100 
        self._bat_data = [0] * 50

    def get_data(self):
        return self._data
    
    def get_bat_data(self):
        return self._bat_data

    def _read_input_registers_safe(self, address, count):
        """
        Intenta leer registros usando los parámetros correctos según la versión de Pymodbus.
        Replica la lógica de los scripts de prueba que funcionaron.
        """
        # INTENTO 1: 'slave' (Estándar Pymodbus v3.x)
        try:
            req = self._client.read_input_registers(address, count, slave=self._modbus_id)
            if not getattr(req, 'isError', lambda: True)():
                return req.registers
        except (TypeError, ValueError):
            pass

        # INTENTO 2: 'unit' (Estándar Pymodbus v2.x - Usado en repos viejos)
        try:
            req = self._client.read_input_registers(address, count, unit=self._modbus_id)
            if not getattr(req, 'isError', lambda: True)():
                return req.registers
        except (TypeError, ValueError):
            pass

        # INTENTO 3: Sin argumentos extra (Fallback)
        try:
            req = self._client.read_input_registers(address, count)
            if not getattr(req, 'isError', lambda: True)():
                return req.registers
        except Exception:
            pass

        return None

    @Throttle(timedelta(seconds=5))
    def update(self):
        """Realiza la conexión y lectura."""
        try:
            if not self._client.connect():
                _LOGGER.error(f"Error conectando a Growatt en {self._host}:{self._port}")
                return
        except Exception as e:
            _LOGGER.error(f"Excepción en conexión: {e}")
            return

        try:
            # 1. LEER BLOQUE PRINCIPAL (Incluye Solar y Grid 3048)
            # Leemos 60 para ir sobrados y asegurar el 3048
            regs = self._read_input_registers_safe(3000, 60)
            if regs:
                self._data = regs
            else:
                _LOGGER.debug("Error leyendo registros 3000+")

            # 2. LEER BLOQUE BATERÍA (3170+)
            regs_bat = self._read_input_registers_safe(3170, 20)
            if regs_bat:
                self._bat_data = regs_bat

        except Exception as e:
            _LOGGER.error(f"Error leyendo datos: {e}")
        finally:
            self._client.close()


class GrowattEmsSensor(Entity):
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
    def name(self): return self._name
    @property
    def state(self): return self._state
    @property
    def unit_of_measurement(self): return self._unit
    @property
    def icon(self): return self._icon
    @property
    def device_class(self): return self._device_class
    @property
    def unique_id(self): return self._attr_unique_id

    def decode_u32(self, data, index):
        if index + 1 < len(data):
            return (data[index] << 16) | data[index + 1]
        return 0

    def decode_s16(self, val):
        if val is not None and (val & 0x8000):
            return val - 0x10000
        return val

    def update(self):
        self._hub.update()
        data = self._hub.get_data()
        bat_data = self._hub.get_bat_data()

        if not data or len(data) < 50:
            return

        # --- LÓGICA DE CÁLCULO (Tu Script Python) ---
        
        # SOLAR (3001)
        p_solar = self.decode_u32(data, 1) * 0.1

        # BATERÍA
        p_bat_charge = 0
        p_bat_discharge = 0
        if bat_data and len(bat_data) > 10:
            p_bat_charge = self.decode_u32(bat_data, 10) * 0.1
            p_bat_discharge = self.decode_u32(bat_data, 8) * 0.1
        p_bat_net = p_bat_charge - p_bat_discharge

        # GRID (3048) con corrección trifásica y signo
        p_grid = 0
        try:
            # Índice 48 = Registro 3048
            raw_3048 = data[48]
            val_3048 = self.decode_s16(raw_3048) * 0.1
            # Tu fórmula mágica:
            p_grid = val_3048 * -1 * math.sqrt(3)
        except (IndexError, TypeError):
            pass

        # LOAD (Consumo casa)
        p_load = p_grid + p_solar - p_bat_net

        # Asignación final
        if self._key == 'solar_power': self._state = round(p_solar, 1)
        elif self._key == 'grid_power': self._state = round(p_grid, 1)
        elif self._key == 'load_power': self._state = round(max(0, p_load), 1)
        elif self._key == 'bat_power': self._state = round(p_bat_net, 1)
        elif self._key == 'bat_soc':
            soc = data[10]
            if soc == 0 and bat_data and len(bat_data) > 1: soc = bat_data[1]
            self._state = soc
