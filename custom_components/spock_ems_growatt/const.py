"""Constantes para la integración Spock EMS Growatt."""

# IMPORTANTE: Coincide con el nombre de la carpeta
DOMAIN = "spock_ems_growatt"

# Config Keys
CONF_SPOCK_API_TOKEN = "spock_api_token"
CONF_SPOCK_PLANT_ID = "spock_plant_id"
CONF_INVERTER_IP = "inverter_ip"
CONF_MODBUS_PORT = "modbus_port"
CONF_MODBUS_ID = "modbus_id"

# NUEVO: Base de potencia para convertir W -> % (carga/descarga)
CONF_BATTERY_MAX_W = "battery_max_w"
DEFAULT_BATTERY_MAX_W = 9000

# Intervalo de ejecución (en segundos)
# Este es el tiempo que espera entre lecturas
SCAN_INTERVAL_SECONDS = 60

# Defaults
DEFAULT_PORT = 502
DEFAULT_MODBUS_ID = 1

# Endpoint
SPOCK_TELEMETRY_API_ENDPOINT = "https://ems-ha.spock.es/api/ems_growatt"

# Plataformas
PLATFORMS = ["sensor"]
