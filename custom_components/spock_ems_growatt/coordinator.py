"""Coordinador de datos para Spock EMS Growatt."""
import logging
import json
import math
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
            timeout=5,
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
            raise ConnectionError(
                f"No se pudo establecer conexión TCP con {self.entry_data[CONF_INVERTER_IP]}"
            )

        # 1. Potencia Nominal (se mantiene como estaba)
        if self.nominal_power_w is None:
            hr = self._read_robust(self.client.read_holding_registers, 10, 1)
            if not hr.isError():
                val = hr.registers[0]
                if 5000 <= val <= 7000:
                    self.nominal_power_w = val / 1000.0
                elif 50000 <= val <= 70000:
                    self.nominal_power_w = val / 10000.0

            if self.nominal_power_w is None:
                ir = self._read_robust(self.client.read_input_registers, 3005, 2)
                if not ir.isError():
                    val = self._decode_u32_be(ir.registers)
                    self.nominal_power_w = val * 0.1 / 1000.0

        # 2. PRODUCCIÓN SOLAR (PV Power) - IR 3001-3002
        ir_pv = self._read_robust(self.client.read_input_registers, 3001, 2)
        if ir_pv.isError():
            raise ModbusException("Error leyendo PV Power (3001)")
        pv_w = self._decode_u32_be(ir_pv.registers) * 0.1

        # 3. RED (GRID) - IR 3048 (s16, 0.1W) CORREGIDO
        # Registro negativo cuando importamos.
        # Multiplicamos por -1 para que Import sea positivo.
        # Multiplicamos por sqrt(3) para ajustar lectura de fase a trifásica.
        ir_grid = self._read_robust(self.client.read_input_registers, 3048, 1)
        if ir_grid.isError():
            raise ModbusException("Error leyendo Grid Power (3048)")
        grid_raw = self._decode_s16(ir_grid.registers[0]) * 0.1
        grid_w = grid_raw * -1 * math.sqrt(3)

        # 4. SOC (3010) con fallback a BMS (3171)
        ir_soc = self._read_robust(self.client.read_input_registers, 3010, 1)
        soc = ir_soc.registers[0] if not ir_soc.isError() else 0
        if soc == 0:
            ir_soc_bms = self._read_robust(self.client.read_input_registers, 3171, 1)
            if not ir_soc_bms.isError():
                soc = ir_soc_bms.registers[0]

        # 5. BATERÍA (registros correctos, como tu script)
        # Discharge: IR 3178-3179 (u32, 0.1W)
        ir_pdis = self._read_robust(self.client.read_input_registers, 3178, 2)
        # Charge:   IR 3180-3181 (u32, 0.1W)
        ir_pch = self._read_robust(self.client.read_input_registers, 3180, 2)

        if ir_pdis.isError() or ir_pch.isError():
            raise ModbusException("Error leyendo potencia de batería (3178/3180)")

        pdis_w = self._decode_u32_be(ir_pdis.registers) * 0.1
        pch_w = self._decode_u32_be(ir_pch.registers) * 0.1

        # Neto batería: positivo = carga, negativo = descarga
        bat_w = pch_w - pdis_w

        # 6. LOAD (Consumo vivienda) = Grid + PV - Battery
        load_w = grid_w + pv_w - bat_w

        # Redondeo al final (como ints para HA/Spock)
        return {
            "battery_soc_total": int(soc),
            "battery_power": int(round(bat_w)),
            "pv_power": int(round(pv_w)),
            "net_grid_power": int(round(grid_w)),
            "supply_power": int(round(load_w)),
        }

    async def _async_update_data(self):
        try:
            data = await self.hass.async_add_executor_job(self._read_modbus_sync)

            _LOGGER.debug("Datos Modbus LEÍDOS: %s", data)

            # Limpieza exhaustiva del ID
            plant_id_clean = str(self.entry_data[CONF_SPOCK_PLANT_ID]).strip()

            spock_payload = {
                "plant_id": plant_id_clean,
                "bat_soc": to_int_str_or_none(data.get("battery_soc_total")),
                "bat_power": to_int_str_or_none(data.get("battery_power")),
                "pv_power": to_int_str_or_none(data.get("pv_power")),
                "ongrid_power": to_int_str_or_none(data.get("net_grid_power")),
                "bat_charge_allowed": "true",
                "bat_discharge_allowed": "true",
                "bat_capacity": "0",
                # OJO: mantengo tu campo tal cual (aunque el nombre suene a energía),
                # ahora al menos llevará el LOAD correcto calculado.
                "total_grid_output_energy": to_int_str_or_none(data.get("supply_power")),
            }

            await self._send_to_spock(spock_payload)
            return data

        except (ModbusException, ConnectionError) as err:
            raise UpdateFailed(f"Error de comunicación Modbus: {err}")
        except Exception as err:
            raise UpdateFailed(f"Error inesperado: {err}")

    async def _send_to_spock(self, payload):
        headers = {
            "X-Auth-Token": str(self.entry_data[CONF_SPOCK_API_TOKEN]).strip(),
            "Content-Type": "application/json",
        }

        serialized_payload = json.dumps(payload)

        _LOGGER.debug("Enviando a Spock (RAW BODY): %s", serialized_payload)

        try:
            async with self.http_session.post(
                SPOCK_TELEMETRY_API_ENDPOINT,
                data=serialized_payload,
                headers=headers,
                timeout=10,
            ) as resp:

                response_text = await resp.text()

                if resp.status != 200:
                    _LOGGER.error("Error HTTP Spock (%s): %s", resp.status, response_text)
                    return

                try:
                    data = await resp.json(content_type=None)
                    _LOGGER.debug("Respuesta de Spock: %s", data)
                except Exception:
                    _LOGGER.warning(
                        "Spock respondió 200 OK pero no es JSON válido: %s", response_text
                    )

        except ClientError as err:
            _LOGGER.error("Error de conexión con Spock API: %s", err)
