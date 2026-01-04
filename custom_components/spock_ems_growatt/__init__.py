"""Inicialización del componente Growatt Spock EMS."""
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, Event
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    PLATFORMS,
)
from .coordinator import GrowattSpockCoordinator

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configura la integración desde la entrada de configuración."""
    
    hass.data.setdefault(DOMAIN, {})

    # 1. Obtener sesión HTTP asíncrona compartida (Best Practice HA)
    # Se usará para el PUSH a Spock
    http_session = async_get_clientsession(hass)

    # 2. Inicializar el Coordinador
    coordinator = GrowattSpockCoordinator(
        hass=hass,
        http_session=http_session,
        entry_data=entry.data
    )

    # 3. Primera actualización de datos (Pull Modbus + Push Spock)
    # Si falla aquí, la integración reintentará en segundo plano
    await coordinator.async_config_entry_first_refresh()

    # 4. Guardar referencia
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator
    }

    # 5. Cargar plataformas (Sensores)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # 6. Registrar listener para cerrar conexión Modbus al apagar HA
    async def _async_handle_shutdown(event: Event) -> None:
        """Cierra el cliente Modbus al detener HA."""
        _LOGGER.debug("Cerrando conexión Modbus Growatt por parada de HA")
        coordinator.close_modbus()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_handle_shutdown)
    )

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Descarga la integración y limpia recursos."""
    
    # Cerrar conexión Modbus explícitamente
    if entry.entry_id in hass.data[DOMAIN]:
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        coordinator.close_modbus()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
