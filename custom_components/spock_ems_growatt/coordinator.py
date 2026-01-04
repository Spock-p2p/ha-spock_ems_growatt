import logging
import requests
import asyncio
from datetime import timedelta
from pymodbus.client import ModbusTcpClient
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

def to_int_str_or_none(value):
    if value is None:
        return None
    try:
        return str(int(round(float(value))))
    except (ValueError, TypeError):
        return None

class GrowattSpockCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry_data):
        super().__init__(
            hass, _LOGGER, name="Growatt Spock Coordinator",
            update_interval=timedelta(seconds=30)
        )
        self.entry_data = entry_data
        self.nominal_power_w = None

    def decode_u32_be(self, regs):
        return (regs[0] << 16) | regs[1] if regs and len(regs) >= 2 else 0

    def decode_s16(self, u16):
        return u16 - 0x10000 if (u16 & 0x8000) else u16

    def fetch_modbus_data(self):
        client = ModbusTcpClient(self.entry_data["inverter_ip"], port=self.entry_data["modbus_port"])
        if not client.connect():
            raise UpdateFailed("Error de conexión Modbus con Growatt")

        try:
            m_id = self.entry_data["modbus_id"]
            
            # Potencia Nominal (HR 6-7)
            if self.nominal_power_w is None:
                hr = client.read_holding_registers(6, count=2, device_id=m_id)
                if not hr.isError():
                    self.nominal_power_w = self.decode_u32_be(hr.registers) * 0.1

            # Telemetría Inmmediata (Input Registers)
            # PV Power (3001)
            ir_pv = client.read_input_registers(3001, count=2, device_id=m_id)
            pv_p = self.decode_u32_be(ir_pv.registers) * 0.1

            # Grid Power (3048) con factor de corrección
            ir_grid = client.read_input_registers(3048, count=1, device_id=m_id)
            grid_raw = self.decode_s16(ir_grid.registers[0]) * 0.1
            net_grid_p = abs(grid_raw) * 3.73

            # SOC y Batería
            ir_soc = client.read_input_registers(3010, count=1, device_id=m_id)
            soc = ir_soc.registers[0]
            if soc == 0:
                soc = client.read_input_registers(3171, count=1, device_id=m_id).registers[0]

            ir_bat = client.read_input_registers(3178, count=4, device_id=m_id)
            pdis_w = self.decode_u32_be(ir_bat.registers[0:2]) * 0.1
            pch_w = self.decode_u32_be(ir_bat.registers[2:4]) * 0.1
            bat_p = pch_w - pdis_w

            return {
                "battery_soc_total": soc,
                "battery_power": bat_p,
                "pv_power": pv_p,
                "net_grid_power": net_grid_p,
                "supply_power": net_grid_p if grid_raw < 0 else 0, # Ejemplo lógica export
            }
        finally:
            client.close()

    async def _async_update_data(self):
        data = await self.hass.async_add_executor_job(self.fetch_modbus_data)
        
        # PAYLOAD IDÉNTICO A SMA
        spock_payload = {
            "plant_id": str(self.entry_data["spock_plant_id"]),
            "bat_soc": to_int_str_or_none(data.get("battery_soc_total")),
            "bat_power": to_int_str_or_none(data.get("battery_power")),
            "pv_power": to_int_str_or_none(data.get("pv_power")),
            "ongrid_power": to_int_str_or_none(data.get("net_grid_power")),
            "bat_charge_allowed": "true",
            "bat_discharge_allowed": "true",
            "bat_capacity": "0",
            "total_grid_output_energy": to_int_str_or_none(data.get("supply_power")),
        }

        await self.send_to_spock(spock_payload)
        return data

    async def send_to_spock(self, payload):
        from .const import SPOCK_TELEMETRY_API_ENDPOINT
        headers = {
            "Authorization": f"Bearer {self.entry_data['spock_api_token']}",
            "Spock-Id": self.entry_data["spock_id"]
        }
        try:
            await self.hass.async_add_executor_job(
                lambda: requests.post(
                    SPOCK_TELEMETRY_API_ENDPOINT, 
                    json=payload, 
                    headers=headers, 
                    timeout=10
                )
            )
        except Exception as e:
            _LOGGER.error("Error Spock API: %s", e)
