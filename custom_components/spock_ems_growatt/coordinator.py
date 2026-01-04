"""Coordinador de datos para Growatt Spock EMS."""
import logging
import asyncio
from datetime import timedelta
from typing import Any, Dict

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException
from aiohttp import ClientSession, ClientError

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    SPOCK_TELEMETRY_API_ENDPOINT,
    CONF_SPOCK_API_TOKEN,
    CONF_SPOCK_ID,
    CONF_SPOCK_PLANT_ID,
    CONF_INVERTER_IP,
    CONF_MODBUS_PORT,
    CONF_MODBUS_ID,
)

_LOGGER = logging.getLogger(__name__)

def to_int_str_or_none(value):
    """Convierte a string entero seguro para JSON."""
    if value is None:
        return None
    try:
        return str(int(round(float(value))))
    except (ValueError, TypeError):
        return None

class GrowattSpockCoordinator(DataUpdateCoordinator):
    """Clase que gestiona el polling a Growatt y el push a Spock."""

    def __init__(self, hass: HomeAssistant, http_session: ClientSession, entry_data: Dict[str, Any]):
        """Inicializa el coordinador."""
        super().__init__(
            hass,
            _LOGGER,
            name="Growatt Spock Coordinator",
            update_interval=timedelta(seconds=30),
        )
        self.http_session = http_session
        self.entry_data = entry_data
        
        # Cliente Modbus (No conectamos todavía)
        self.client = ModbusTcpClient(
            host=entry_data[CONF_INVERTER_IP],
            port=entry_data[CONF_MODBUS_PORT],
            timeout=5
        )
        self.modbus_id = entry_data[CONF_MODBUS_ID]
        
        # Variable para almacenar potencia nominal (Leída una sola vez)
        self.nominal_power_w = None

    def close_modbus(self):
        """Cierra la conexión Modbus TCP."""
        if self.client:
            self.client.close()

    def _decode_u32_be(self, regs):
        return (regs[0] << 16) | regs[1] if regs and len(regs) >= 2 else 0

    def _decode_s16(self, u16):
        return u16 - 0x10000 if (u16 & 0x8000) else u16

    def _read_modbus_sync(self) -> Dict[str, Any]:
        """Lectura síncrona de registros Modbus (Ejecutar en Executor)."""
        if not self.client.connect():
            raise ConnectionError("No se pudo establecer conexión TCP con el inversor")

        # 1. Potencia Nominal (Solo si no la tenemos)
        if self.nominal_power_w is None:
            # HR 10 (Growatt standard) o IR 3005 fallback
            hr = self.client.read_holding_registers(10, count=1, slave=self.modbus_id)
            if not hr.isError():
                val = hr.registers[0]
                # Lógica de detección de escala
                if 5000 <= val <= 7000: self.nominal_power_w = val / 1000.0  # W -> kW
                elif 50000 <= val <= 70000: self.nominal_power_w = val / 10000.0 # 0.1W -> kW
            
            # Fallback a Input Register 3005
            if self.nominal_power_w is None:
                 ir = self.client.read_input_registers(3005, count=2, slave=self.modbus_id)
                 if not ir.isError():
                     val = self._decode_u32_be(ir.registers)
                     self.nominal_power_w = val * 0.1 / 1000.0

        # 2. Telemetría
        # PV Power (3001-3002)
        ir_pv = self.client.read_input_registers(3001, count=2, slave=self.modbus_id)
        if ir_pv.isError(): raise ModbusException("Error leyendo PV Power")
        pv_p = self._decode_u32_be(ir_pv.registers) * 0.1

        # Grid Power (3048)
        ir_grid = self.client.read_input_registers(3048, count=1, slave=self.modbus_id)
        if ir_grid.isError(): raise ModbusException("Error leyendo Grid Power")
        grid_raw = self._decode_s16(ir_grid.registers[0]) * 0.1
        net_grid_p = abs(grid_raw) * 3.73 # Factor de corrección trifásico

        # SOC (3010 preferido, 3171 fallback)
        ir_soc = self.client.read_input_registers(3010, count=1, slave=self.modbus_id)
        soc = ir_soc.registers[0] if not ir_soc.isError() else 0
        if soc == 0:
            ir_soc_bms = self.client.read_input_registers(3171, count=1, slave=self.modbus_id)
            if not ir_soc_bms.isError():
                soc = ir_soc_bms.registers[0]

        # Batería Power (3180 Charge / 3178 Discharge)
        ir_bat = self.client.read_input_registers(3178, count=4, slave=self.modbus_id)
        if ir_bat.isError(): raise ModbusException("Error leyendo Batería")
        pdis_w = self._decode_u32_be(ir_bat.registers[0:2]) * 0.1
        pch_w = self._decode_u32_be(ir_bat.registers[2:4]) * 0.1
        bat_p = pch_w - pdis_w

        return {
            "battery_soc_total": soc,
            "battery_power": bat_p,
            "pv_power": pv_p,
            "net_grid_power": net_grid_p,
            "supply_power": 0, # Placeholder por si se requiere export separado
        }

    async def _async_update_data(self):
        """Orquesta la actualización: Pull Modbus -> Push Spock."""
        try:
            # PULL: Ejecutar lectura bloqueante en thread pool
            data = await self.hass.async_add_executor_job(self._read_modbus_sync)
            
            # Preparar Payload
            spock_payload = {
                "plant_id": str(self.entry_data[CONF_SPOCK_PLANT_ID]),
                "bat_soc": to_int_str_or_none(data.get("battery_soc_total")),
                "bat_power": to_int_str_or_none(data.get("battery_power")),
                "pv_power": to_int_str_or_none(data.get("pv_power")),
                "ongrid_power": to_int_str_or_none(data.get("net_grid_power")),
                "bat_charge_allowed": "true", # Hardcoded por ahora
                "bat_discharge_allowed": "true", # Hardcoded por ahora
                "bat_capacity": "0",
                "total_grid_output_energy": to_int_str_or_none(data.get("supply_power")),
            }

            # PUSH: Enviar a Spock de forma asíncrona
            await self._send_to_spock(spock_payload)

            return data

        except (ModbusException, ConnectionError) as err:
            raise UpdateFailed(f"Error de comunicación Modbus: {err}")
        except Exception as err:
            raise UpdateFailed(f"Error inesperado: {err}")

    async def _send_to_spock(self, payload):
        """Envía telemetría a la API de Spock usando aiohttp."""
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
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Spock API respondió con error: %s", resp.status)
                # Aquí leeremos resp.json() cuando implementemos comandos
        except ClientError as err:
            _LOGGER.error("Error de conexión con Spock API: %s", err)
