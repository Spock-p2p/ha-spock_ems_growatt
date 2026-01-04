import logging
import asyncio
from datetime import timedelta
from pymodbus.client import ModbusTcpClient
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.core import HomeAssistant
from aiohttp import ClientSession

from .const import SPOCK_TELEMETRY_API_ENDPOINT, CONF_SPOCK_API_TOKEN, CONF_SPOCK_ID, CONF_SPOCK_PLANT_ID

_LOGGER = logging.getLogger(__name__)

def to_int_str_or_none(value):
    if value is None:
        return None
    try:
        return str(int(round(float(value))))
    except (ValueError, TypeError):
        return None

class GrowattSpockCoordinator(DataUpdateCoordinator):
    """Coordinador para gestionar lectura Modbus y envío a Spock."""

    def __init__(self, hass: HomeAssistant, http_session: ClientSession, entry_data: dict):
        super().__init__(
            hass, _LOGGER, name="Growatt Spock Coordinator",
            update_interval=timedelta(seconds=30)
        )
        self.http_session = http_session
        self.entry_data = entry_data
        
        # Cliente Modbus persistente (se conecta/desconecta según necesidad o se mantiene)
        # Para robustez en HA, a veces es mejor conectar por ciclo, o mantener keep-alive.
        # Aquí instanciamos el cliente.
        self.client = ModbusTcpClient(
            entry_data["inverter_ip"], 
            port=entry_data["modbus_port"]
        )
        self.nominal_power_w = None

    def decode_u32_be(self, regs):
        return (regs[0] << 16) | regs[1] if regs and len(regs) >= 2 else 0

    def decode_s16(self, u16):
        return u16 - 0x10000 if (u16 & 0x8000) else u16

    def fetch_modbus_data(self):
        """Lectura síncrona de Modbus (se ejecuta en thread pool)."""
        if not self.client.connect():
            raise UpdateFailed("Error de conexión Modbus con Growatt")

        try:
            m_id = self.entry_data["modbus_id"]
            
            # --- 1. Potencia Nominal ---
            if self.nominal_power_w is None:
                # Holding Register 10 o 3005 según tu script final
                hr = self.client.read_holding_registers(10, count=1, device_id=m_id)
                if not hr.isError():
                     val = hr.registers[0]
                     # Lógica simplificada de tu script
                     if 5000 <= val <= 7000: self.nominal_power_w = val / 1000.0
                     elif 50000 <= val <= 70000: self.nominal_power_w = val / 10000.0
            
            # --- 2. Telemetría ---
            # PV (3001)
            ir_pv = self.client.read_input_registers(3001, count=2, device_id=m_id)
            pv_p = self.decode_u32_be(ir_pv.registers) * 0.1

            # Grid (3048) + Factor 3.73
            ir_grid = self.client.read_input_registers(3048, count=1, device_id=m_id)
            grid_raw = self.decode_s16(ir_grid.registers[0]) * 0.1
            net_grid_p = abs(grid_raw) * 3.73

            # SOC (3010 o 3171)
            ir_soc = self.client.read_input_registers(3010, count=1, device_id=m_id)
            soc = ir_soc.registers[0]
            if soc == 0:
                soc = self.client.read_input_registers(3171, count=1, device_id=m_id).registers[0]

            # Batería Power (3180/3178)
            ir_bat = self.client.read_input_registers(3178, count=4, device_id=m_id)
            pdis_w = self.decode_u32_be(ir_bat.registers[0:2]) * 0.1
            pch_w = self.decode_u32_be(ir_bat.registers[2:4]) * 0.1
            bat_p = pch_w - pdis_w

            # Supply Power (Exportación)
            # Si grid_raw es negativo en Growatt suele ser Import. 
            # Aquí asumimos lógica: Si (net_grid_p) es lo que intercambia...
            # Ajustaremos supply a 0 por seguridad si importamos.
            supply_p = 0 # Implementar lógica real de exportación si necesaria

            return {
                "battery_soc_total": soc,
                "battery_power": bat_p,
                "pv_power": pv_p,
                "net_grid_power": net_grid_p,
                "supply_power": supply_p,
            }
        except Exception as e:
            raise UpdateFailed(f"Error leyendo Modbus: {e}")
        # No cerramos cliente aquí para permitir reutilización o keep-alive si se desea,
        # pero la conexión Modbus TCP puede ser caprichosa.
        # Si prefieres cerrar siempre: self.client.close()

    async def _async_update_data(self):
        """Orquesta la lectura y el envío a Spock."""
        # 1. PULL Modbus (en executor porque pymodbus es sync)
        data = await self.hass.async_add_executor_job(self.fetch_modbus_data)
        
        # 2. Preparar Payload SMA-Style
        spock_payload = {
            "plant_id": str(self.entry_data[CONF_SPOCK_PLANT_ID]),
            "bat_soc": to_int_str_or_none(data.get("battery_soc_total")),
            "bat_power": to_int_str_or_none(data.get("battery_power")),
            "pv_power": to_int_str_or_none(data.get("pv_power")),
            "ongrid_power": to_int_str_or_none(data.get("net_grid_power")),
            "bat_charge_allowed": "true",
            "bat_discharge_allowed": "true",
            "bat_capacity": "0",
            "total_grid_output_energy": to_int_str_or_none(data.get("supply_power")),
        }

        # 3. PUSH Spock (Asíncrono con aiohttp)
        await self.send_to_spock(spock_payload)
        
        return data

    async def send_to_spock(self, payload):
        """Envía telemetría a Spock usando aiohttp (Non-blocking)."""
        headers = {
            "Authorization": f"Bearer {self.entry_data[CONF_SPOCK_API_TOKEN]}",
            "Spock-Id": self.entry_data[CONF_SPOCK_ID]
        }
        
        try:
            async with self.http_session.post(
                SPOCK_TELEMETRY_API_ENDPOINT, 
                json=payload, 
                headers=headers,
                timeout=10
            ) as response:
                if response.status != 200:
                    _LOGGER.warning(f"Spock API error: {response.status}")
                # Aquí se leería la respuesta para comandos futuros
                # await response.json()
        except Exception as e:
            _LOGGER.error(f"Error enviando a Spock: {e}")
