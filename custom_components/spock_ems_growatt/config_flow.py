import logging
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from pymodbus.client import ModbusTcpClient

from .const import (
    DOMAIN,
    CONF_SPOCK_ID,
    CONF_SPOCK_API_TOKEN,
    CONF_SPOCK_PLANT_ID,
    CONF_INVERTER_IP,
    CONF_MODBUS_PORT,
    CONF_MODBUS_ID,
    DEFAULT_PORT,
    DEFAULT_MODBUS_ID,
)

_LOGGER = logging.getLogger(__name__)

# Esquema base para la configuración inicial
DATA_SCHEMA = vol.Schema({
    vol.Required(CONF_SPOCK_ID): str,
    vol.Required(CONF_SPOCK_API_TOKEN): str,
    vol.Required(CONF_SPOCK_PLANT_ID): str,
    vol.Required(CONF_INVERTER_IP): str,
    vol.Optional(CONF_MODBUS_PORT, default=DEFAULT_PORT): int,
    vol.Optional(CONF_MODBUS_ID, default=DEFAULT_MODBUS_ID): int,
})

async def validate_input(hass, data: dict):
    """
    Valida que los datos introducidos por el usuario sean correctos.
    Intenta conectar vía Modbus TCP para verificar IP y Puerto.
    """
    ip = data[CONF_INVERTER_IP]
    port = data[CONF_MODBUS_PORT]
    # slave = data[CONF_MODBUS_ID] # No es crítico para conectar, pero sí para leer

    client = ModbusTcpClient(ip, port=port)
    
    # Ejecutamos la conexión en un hilo aparte porque pymodbus es bloqueante
    is_connected = await hass.async_add_executor_job(client.connect)
    
    if not is_connected:
        client.close()
        raise CannotConnect

    # Opcional: Podríamos intentar leer un registro para asegurar que es un Growatt,
    # pero con confirmar conexión TCP es suficiente para la validación básica.
    client.close()

    return {"title": f"Growatt {ip}"}


class GrowattSpockConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Flujo de configuración inicial para Growatt Spock EMS."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Habilita el botón 'Configurar' en integraciones."""
        return GrowattSpockOptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        """Maneja el paso inicial del usuario."""
        errors = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)

                # Usamos la IP como identificador único para no duplicar entradas
                await self.async_set_unique_id(user_input[CONF_INVERTER_IP])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(title=info["title"], data=user_input)

            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception as e:
                _LOGGER.error("Error desconocido: %s", e)
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )


class GrowattSpockOptionsFlow(config_entries.OptionsFlow):
    """
    Maneja la reconfiguración (Options Flow).
    Permite cambiar IP, Tokens o IDs sin reinstalar.
    """

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Muestra el formulario con los valores actuales."""
        errors = {}
        
        # Datos actuales guardados en la configuración
        current_config = self.config_entry.data

        if user_input is not None:
            try:
                # Validamos de nuevo la conexión con los nuevos datos
                await validate_input(self.hass, user_input)

                # Actualizamos la entrada de configuración existente
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=user_input
                )

                # Recargamos la integración para aplicar cambios inmediatamente
                await self.hass.config_entries.async_reload(self.config_entry.entry_id)

                return self.async_create_entry(title="", data={})

            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception as e:
                _LOGGER.error("Error reconfigurando: %s", e)
                errors["base"] = "unknown"

        # Esquema con valores por defecto pre-rellenos (igual que SMA)
        options_schema = vol.Schema({
            vol.Required(CONF_SPOCK_ID, default=current_config.get(CONF_SPOCK_ID)): str,
            vol.Required(CONF_SPOCK_API_TOKEN, default=current_config.get(CONF_SPOCK_API_TOKEN)): str,
            vol.Required(CONF_SPOCK_PLANT_ID, default=current_config.get(CONF_SPOCK_PLANT_ID)): str,
            vol.Required(CONF_INVERTER_IP, default=current_config.get(CONF_INVERTER_IP)): str,
            vol.Optional(CONF_MODBUS_PORT, default=current_config.get(CONF_MODBUS_PORT, DEFAULT_PORT)): int,
            vol.Optional(CONF_MODBUS_ID, default=current_config.get(CONF_MODBUS_ID, DEFAULT_MODBUS_ID)): int,
        })

        return self.async_show_form(
            step_id="init", data_schema=options_schema, errors=errors
        )


class CannotConnect(Exception):
    """Error para indicar que no se pudo conectar al Host."""
