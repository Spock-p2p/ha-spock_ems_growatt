"""Coordinador de datos para Spock EMS Growatt (MOD 6000TL3-XH)."""
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
            timeout=5,
        )

        self.nominal_power_w = None

    def close_modbus(self):
        if self.client:
            self.client.close()

    # ---- Decoders ----
    def _decode_u32_be(self, regs):
        return (regs[0] << 16) | regs[1] if regs and len(regs) >= 2 else 0

    def _read_robust(self, func, address, count):
        """Mecanismo de lectura UNIVERSAL."""
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

        # 1) Potencia nominal (opcional, solo informativa)
        if self.nominal_power_w is None:
            # HR6/HR7 (u32, 0.1W) en tu equipo funciona
            hr = self._read_robust(self.client.read_holding_registers, 6, 2)
            if not hr.isError():
                raw = self._decode_u32_be(hr.registers)
                val_w = raw * 0.1
                if 500 <= val_w <= 50000:
                    self.nominal_power_w = val_w

        # 2) PV Power (IR 3001-3002) u32 * 0.1W
        ir_pv = self._read_robust(self.client.read_input_registers, 3001, 2)
        if ir_pv.isError():
            raise ModbusException("Error leyendo PV Power (3001)")
        pv_w = self._decode_u32_be(ir_pv.registers) * 0.1
        pv_p = int(round(pv_w))

        # 3) GRID / LOAD correctos (según protocolo de tu modelo)
        # Ptouser total (IR3041-3042) u32 * 0.1W  => Import total real
        ir_ptouser = self._read_robust(self.client.read_input_registers, 3041, 2)
        if ir_ptouser.isError():
            raise ModbusException("Error leyendo Ptouser total (3041)")
        ptouser_w = self._decode_u32_be(ir_ptouser.registers) * 0.1

        # Ptogrid total (IR3043-3044) u32 * 0.1W  => Export total
        ir_ptogrid = self._read_robust(self.client.read_input_registers, 3043, 2)
        if ir_ptogrid.isError():
            raise ModbusException("Error leyendo Ptogrid total (3043)")
        ptogrid_w = self._decode_u32_be(ir_ptogrid.registers) * 0.1

        # Ptoload total (IR3045-3046) u32 * 0.1W  => Load real
        ir_ptoload = self._read_robust(self.client.read_input_registers, 3045, 2)
        if ir_ptoload.isError():
            raise ModbusException("Error leyendo Ptoload total (3045)")
        ptoload_w = self._decode_u32_be(ir_ptoload.registers) * 0.1

        # Net grid: import - export (positivo = import)
        net_grid_w = ptouser_w - ptogrid_w

        # 4) SOC
        ir_soc = self._read_robust(self.client.read_input_registers, 3010, 1)
        soc = ir_soc.registers[0] if not ir_soc.isError() else 0
        if soc == 0:
            ir_soc_bms = self._read_robust(self.client.read_input_registers, 3171, 1)
            if not ir_soc_bms.isError():
                soc = ir_soc_bms.registers[0]

        # 5) Batería: pdis (3178-3179) y pch (3180-3181) u32*0.1W
        ir_pdis = self._read_robust(self.client.read_input_registers, 3178, 2)
        if ir_pdis.isError():
            raise ModbusException("Error leyendo Battery Discharge Power (3178)")
        pdis_w = self._decode_u32_be(ir_pdis.registers) * 0.1

        ir_pch = self._read_robust(self.client.read_input_registers, 3180, 2)
        if ir_pch.isError():
            raise ModbusException("Error leyendo Battery Charge Power (3180)")
        pch_w = self._decode_u32_be(ir_pch.registers) * 0.1

        bat_net_w = pch_w - pdis_w
        bat_p = int(round(bat_net_w))

        load_p = int(round(ptoload_w))

        return {
            "battery_soc_total": int(soc),
            "battery_power": bat_p,          # + carga / - descarga
            "pv_power": pv_p,
            "net_grid_power": int(round(net_grid_w)),  # + import / - export
            "load_power": load_p,            # carga real vivienda
        }

    async def _async_update_data(self):
        try:
            data = await self.hass.async_add_executor_job(self._read_modbus_sync)

            _LOGGER.debug("Datos Modbus LEÍDOS: %s", data)

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
                # Si Spock espera “total_grid_output_energy” y tú lo usabas para “supply/load”,
                # aquí es mejor enviar el LOAD real:
                "total_grid_output_energy": to_int_str_or_none(data.get("load_power")),
            }

            await self._send_to_spock(spock_payload)
            return data

        except (ModbusException, ConnectionError) as err:
            raise UpdateFailed(f"Error de comunicación Modbus: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Error inesperado: {err}") from err

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
                    _LOGGER.warning("Spock respondió 200 OK pero no es JSON válido: %s", response_text)

        except ClientError as err:
            _LOGGER.error("Error de conexión con Spock API: %s", err)
