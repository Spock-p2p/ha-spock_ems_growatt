"""Config flow para Spock EMS Growatt."""
import logging
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from pymodbus.client import ModbusTcpClient

from .const import (
    DOMAIN,
    CONF_SPOCK_API_TOKEN,
    CONF_SPOCK_PLANT_ID,
    CONF_INVERTER_IP,
    CONF_MODBUS_PORT,
    CONF_MODBUS_ID,
    CONF_BATTERY_MAX_W,
    DEFAULT_BATTERY_MAX_W,
    DEFAULT_PORT,
    DEFAULT_MODBUS_ID,
)

_LOGGER = logging.getLogger(__name__)

# Schema (incluye battery_max_w con default 9000)
DATA_SCHEMA = vol.Schema({
    vol.Required(CONF_SPOCK_API_TOKEN): str,
    vol.Required(CONF_SPOCK_PLANT_ID): str,
    vol.Required(CONF_INVERTER_IP): str,
    vol.Optional(CONF_MODBUS_PORT, default=DEFAULT_PORT): vol.Coerce(int),
    vol.Optional(CONF_MODBUS_ID, default=DEFAULT_MODBUS_ID): vol.Coerce(int),
    vol.Optional(CONF_BATTERY_MAX_W, default=DEFAULT_BATTERY_MAX_W): vol.Coerce(int),
})

class CannotConnect(Exception):
    """Error indicando fallo de conexión."""

async def validate_input(hass, data: dict):
    """Valida la conexión Modbus TCP."""
    client = ModbusTcpClient(data[CONF_INVERTER_IP], port=data[CONF_MODBUS_PORT])
    is_connected = await hass.async_add_executor_job(client.connect)
    client.close()

    if not is_connected:
        raise CannotConnect

    # Validación simple del battery_max_w (no bloquea conexión, pero evita valores absurdos)
    try:
        bmw = int(data.get(CONF_BATTERY_MAX_W, DEFAULT_BATTERY_MAX_W))
        if bmw <= 0:
            raise ValueError
    except Exception:
        # Si el usuario mete algo raro, lo normalizamos al default
        data[CONF_BATTERY_MAX_W] = DEFAULT_BATTERY_MAX_W

    return {"title": f"Growatt {data[CONF_INVERTER_IP]}"}

class GrowattSpockConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Flujo de configuración inicial."""
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return GrowattSpockOptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
                await self.async_set_unique_id(user_input[CONF_INVERTER_IP])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info["title"], data=user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"

        return self.async_show_form(step_id="user", data_schema=DATA_SCHEMA, errors=errors)

class GrowattSpockOptionsFlow(config_entries.OptionsFlow):
    """Flujo de reconfiguración (Options)."""

    def __init__(self, config_entry):
        # Usamos variable privada para no chocar con la propiedad 'config_entry'
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                await validate_input(self.hass, user_input)
                # Seguimos tu enfoque actual: reconfiguramos entry.data
                self.hass.config_entries.async_update_entry(self._config_entry, data=user_input)
                await self.hass.config_entries.async_reload(self._config_entry.entry_id)
                return self.async_create_entry(title="", data={})
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"

        current = self._config_entry.data
        schema = vol.Schema({
            vol.Required(CONF_SPOCK_API_TOKEN, default=current.get(CONF_SPOCK_API_TOKEN)): str,
            vol.Required(CONF_SPOCK_PLANT_ID, default=current.get(CONF_SPOCK_PLANT_ID)): str,
            vol.Required(CONF_INVERTER_IP, default=current.get(CONF_INVERTER_IP)): str,
            vol.Optional(CONF_MODBUS_PORT, default=current.get(CONF_MODBUS_PORT, DEFAULT_PORT)): vol.Coerce(int),
            vol.Optional(CONF_MODBUS_ID, default=current.get(CONF_MODBUS_ID, DEFAULT_MODBUS_ID)): vol.Coerce(int),
            vol.Optional(CONF_BATTERY_MAX_W, default=current.get(CONF_BATTERY_MAX_W, DEFAULT_BATTERY_MAX_W)): vol.Coerce(int),
        })

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
