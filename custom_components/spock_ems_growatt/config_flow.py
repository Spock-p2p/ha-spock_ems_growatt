import voluptuous as vol
from homeassistant import config_entries
from .const import (
    DOMAIN, CONF_SPOCK_ID, CONF_SPOCK_API_TOKEN, CONF_SPOCK_PLANT_ID,
    CONF_INVERTER_IP, CONF_MODBUS_PORT, CONF_MODBUS_ID, DEFAULT_PORT, DEFAULT_MODBUS_ID
)

class GrowattSpockConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title=f"Growatt {user_input[CONF_INVERTER_IP]}", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_SPOCK_ID): str,
                vol.Required(CONF_SPOCK_API_TOKEN): str,
                vol.Required(CONF_SPOCK_PLANT_ID): str,
                vol.Required(CONF_INVERTER_IP): str,
                vol.Optional(CONF_MODBUS_PORT, default=DEFAULT_PORT): int,
                vol.Optional(CONF_MODBUS_ID, default=DEFAULT_MODBUS_ID): int,
            })
        )
