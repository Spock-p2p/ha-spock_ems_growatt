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
    DEFAULT_PORT,
    DEFAULT_MODBUS_ID,
)

_LOGGER = logging.getLogger(__name__)

# Schema
DATA_SCHEMA = vol.Schema({
    vol.Required(CONF_SPOCK_API_TOKEN): str,
    vol.Required(CONF_SPOCK_PLANT_ID): str,
    vol.Required(CONF_INVERTER_IP): str,
    vol.Optional(CONF_MODBUS_PORT, default=DEFAULT_PORT): int,
    vol.Optional(CONF_MODBUS_ID, default=DEFAULT_MODBUS_ID): int,
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
    
    # Eliminado __init__ para corregir warning de deprecación.
    
    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                await validate_input(self.hass, user_input)
                self.hass.config_entries.async_update_entry(self.config_entry, data=user_input)
                await self.hass.config_entries.async_reload(self.config_entry.entry_id)
                return self.async_create_entry(title="", data={})
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"

        current = self.config_entry.data
        schema = vol.Schema({
            vol.Required(CONF_SPOCK_API_TOKEN, default=current.get(CONF_SPOCK_API_TOKEN)): str,
            vol.Required(CONF_SPOCK_PLANT_ID, default=current.get(CONF_SPOCK_PLANT_ID)): str,
            vol.Required(CONF_INVERTER_IP, default=current.get(CONF_INVERTER_IP)): str,
            vol.Optional(CONF_MODBUS_PORT, default=current.get(CONF_MODBUS_PORT, DEFAULT_PORT)): int,
            vol.Optional(CONF_MODBUS_ID, default=current.get(CONF_MODBUS_ID, DEFAULT_MODBUS_ID)): int,
        })

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
