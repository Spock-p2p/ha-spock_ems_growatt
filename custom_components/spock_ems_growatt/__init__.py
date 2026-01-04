import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, Event
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    PLATFORMS,
    CONF_SPOCK_API_TOKEN,
    CONF_SPOCK_PLANT_ID,
    CONF_SPOCK_ID,
    CONF_INVERTER_IP,
    CONF_MODBUS_PORT,
    CONF_MODBUS_ID,
    SPOCK_TELEMETRY_API_ENDPOINT,
)
from .coordinator import GrowattSpockCoordinator

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configura la integración desde la Config Entry."""
    
    config = entry.data
    hass.data.setdefault(DOMAIN, {})

    # 1. Sesión HTTP compartida de HA (para el PUSH a Spock)
    # Esto sustituye a 'requests' y es mucho más eficiente
    http_session = async_get_clientsession(hass)

    # 2. Inicializar el Coordinador
    # Le pasamos la sesión HTTP y todos los datos necesarios
    coordinator = GrowattSpockCoordinator(
        hass=hass,
        http_session=http_session,
        entry_data=config
    )

    # 3. Primera carga de datos (PULL Growatt + PUSH Spock)
    await coordinator.async_config_entry_first_refresh()

    # 4. Guardar referencia en hass.data
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator
    }

    # 5. Registrar plataformas (sensores)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # 6. Manejo de cierre (Shutdown)
    async def _async_handle_shutdown(event: Event) -> None:
        """Cierra conexiones al apagar HA."""
        if coordinator.client:
            coordinator.client.close()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_handle_shutdown)
    )

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Descarga la integración."""
    
    # Recuperamos el coordinador para cerrar conexión Modbus
    data = hass.data[DOMAIN].get(entry.entry_id)
    if data:
        coordinator = data["coordinator"]
        if coordinator.client:
            coordinator.client.close()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
