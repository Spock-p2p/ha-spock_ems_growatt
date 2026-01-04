"""Coordinador de datos para Spock EMS Growatt."""
import logging
import json
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
    CONF_SPOCK_PLANT_ID,
    CONF_INVERTER_IP,
    CONF_MODBUS_PORT,
    CONF_MODBUS_ID,
    SCAN_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

def to_int_str_or_none(value):
    if value is None:
        return None
    try:
        return str(int(round(float(value))))
    except (ValueError, TypeError):
        return None

class GrowattSpockCoordinator(DataUpdateCoordinator):
    """Clase que gestiona el polling a Growatt y el push a Spock."""

    def __init__(self, hass: HomeAssistant, http_session: ClientSession, entry_data: Dict[str, Any]):
        super().__init__(
            hass,
            _LOGGER,
            name="Growatt Spock Coordinator",
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self.http_session = http_session
        self.entry_data = entry_data
        
        self.modbus_id = int(entry_data[CONF_MODBUS_ID])
        
        self.client = ModbusTcpClient(
            host=entry_data[CONF_INVERTER_IP],
            port=int(entry_data[CONF_MODBUS_PORT]),
            timeout=5
        )
        self.nominal_power_w = None

    def close_modbus(self):
        if self.client:
            self.client.close()

    def _decode_u32_be(self, regs):
        return (regs[0] << 16) | regs[1] if regs and len(regs) >= 2 else 0

    def _decode_s16(self, u16):
        return u16 - 0x10000 if (u16 & 0x8000) else u16

    def _read_robust(self, func, address, count):
        """Mecanismo de lectura UNIVERSAL (Estilo SMA Robust)."""
        try:
            return func(address, count=count, device_id=self.modbus_id)
        except TypeError:
            pass
        try:
            return func(address, count=count, slave=self.modbus_id)
        except TypeError:
            pass
        return func(address, count=count, unit=self.modbus_id)

    def _read_modbus_sync(self) -> Dict[str, Any]:
        """Lectura síncrona de registros Modbus."""
        if not self.client.connect():
            raise ConnectionError(f"No se pudo establecer conexión TCP con {self.entry_data[CONF_INVERTER_IP]}")

        # 1. Potencia Nominal
        if self.nominal_power_w is None:
            hr = self._read_robust(self.client.read_holding_registers, 10, 1)
            if not hr.isError():
                val = hr.registers[0]
                if 5000 <= val <= 7000: self.nominal_power_w = val / 1000.0
                elif 50000 <= val <= 70000: self.nominal_power_w = val / 10000.0
            
            if self.nominal_power_w is None:
                 ir = self._read_robust(self.client.read_input_registers, 3005, 2)
                 if not ir.isError():
                     val = self._decode_u32_be(ir.registers)
                     self.nominal_power_w = val * 0.1 / 1000.0

        # 2. Telemetría - CON REDONDEO A ENTEROS
        ir_pv = self._read_robust(self.client.read_input_registers, 3001, 2)
        if ir_pv.isError(): raise ModbusException("Error leyendo PV Power (3001)")
        pv_p = int(round(self._decode_u32_be(ir_pv.registers) * 0.1))

        ir_grid = self._read_robust(self.client.read_input_registers, 3048, 1)
        if ir_grid.isError(): raise ModbusException("Error leyendo Grid Power (3048)")
        grid_raw = self._decode_s16(ir_grid.registers[0]) * 0.1
        net_grid_p = int(round(abs(grid_raw) * 3.73))

        ir_soc = self._read_robust(self.client.read_input_registers, 3010, 1)
        soc = ir_soc.registers[0] if not ir_soc.isError() else 0
        if soc == 0:
            ir_soc_bms = self._read_robust(self.client.read_input_registers, 3171, 1)
            if not ir_soc_bms.isError():
                soc = ir_soc_bms.registers[0]

        ir_bat = self._read_robust(self.client.read_input_registers, 3178, 4)
        if ir_bat.isError(): raise ModbusException("Error leyendo Batería (3178)")
        pdis_w = self._decode_u32_be(ir_bat.registers[0:2]) * 0.1
        pch_w = self._decode_u32_be(ir_bat.registers[2:4]) * 0.1
        bat_p = int(round(pch_w - pdis_w))

        return {
            "battery_soc_total": int(soc),
            "battery_power": bat_p,
            "pv_power": pv_p,
            "net_grid_power": net_grid_p,
            "supply_power": 0,
        }

    async def _async_update_data(self):
        try:
            data = await self.hass.async_add_executor_job(self._read_modbus_sync)
            
            _LOGGER.debug("Datos Modbus LEÍDOS: %s", data)

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

            _LOGGER.debug("Payload enviando a SPOCK: %s", spock_payload)

            await self._send_to_spock(spock_payload)
            return data

        except (ModbusException, ConnectionError) as err:
            raise UpdateFailed(f"Error de comunicación Modbus: {err}")
        except Exception as err:
            raise UpdateFailed(f"Error inesperado: {err}")

    async def _send_to_spock(self, payload):
        headers = {
            "Authorization": f"Bearer {self.entry_data[CONF_SPOCK_API_TOKEN]}",
            "Content-Type": "application/json"
        }
        
        try:
            async with self.http_session.post(
                SPOCK_TELEMETRY_API_ENDPOINT,
                json=payload,
                headers=headers,
                timeout=10
            ) as resp:
                
                # --- LEER RESPUESTA (Igual que en SMA) ---
                response_text = await resp.text()
                
                if resp.status != 200:
                    _LOGGER.error("Error HTTP Spock (%s): %s", resp.status, response_text)
                    return # Salimos si hay error
                
                # Si es 200, intentamos parsear JSON para debug y futuras órdenes
                try:
                    data = await resp.json(content_type=None)
                    _LOGGER.debug("Respuesta de Spock: %s", data)
                    
                    # AQUÍ IRÁ LA LÓGICA DE CONTROL DE BATERÍA (FUTURO)
                    # if data.get("operation_mode") ...
                    
                except Exception as e:
                    _LOGGER.warning("Spock respondió 200 OK pero no es JSON válido: %s", response_text)

        except ClientError as err:
            _LOGGER.error("Error de conexión con Spock API: %s", err)
