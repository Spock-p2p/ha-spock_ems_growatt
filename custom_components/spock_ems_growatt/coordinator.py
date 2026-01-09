"""Coordinador de datos para Spock EMS Growatt.

IMPORTANTE
- La telemetría (lecturas Modbus + payload a Spock) mantiene la misma lógica/cálculos.
- Se añade control (acciones) a partir del JSON de respuesta de Spock.

Seguridad (igual que tus scripts):
- Escrituras siempre con FC16 (write_registers).
- Backup + readback + rollback best-effort.
- Chequeo de batería online antes y después.
- Evita re-escribir si el comando no ha cambiado (reduce riesgo y tráfico).
"""

import logging
import json
import math
import time
import threading
from datetime import timedelta
from typing import Any, Dict, Optional, Tuple

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
    CONF_BATTERY_MAX_W,
    DEFAULT_BATTERY_MAX_W,
    SCAN_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

# Delays “anti-problemas” (mismos que tus scripts)
_DELAY_AFTER_WRITE_S = 1.5
_READBACK_RETRIES = 8
_READBACK_SLEEP_S = 0.4

# Registros TOU / Control (holding)
_T1_CFG = 3038
_T1_END = 3039
_T2_CFG = 3040
_T2_END = 3041
_T3_CFG = 3042
_T3_END = 3043

_HR_CHARGE_POWER_RATE = 3047      # Charging Power Rate (%)
_HR_GRID_CHARGE_ENABLE = 3049     # Grid Charging (1/0)
_HR_DISCHARGE_POWER_RATE = 3036   # Discharging Power Rate (%)

# Máscaras / bits TOU
_ENABLE_BIT = 0x8000
_MODE_MASK = 0x6000
_TIME_BITS_MASK = 0x1FFF

_MODE_LOAD = 0x0000
_MODE_BAT = 0x2000
_MODE_GRID = 0x4000

# Lecturas batería (input)
_IR_BDC_STATE = 3118
_IR_VBAT = 3169
_IR_SOC = 3171
_IR_PDIS_H = 3178  # u32 0.1W
_IR_PCH_H = 3180   # u32 0.1W


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

        # NUEVO: configurable desde el módulo (default 9000)
        try:
            self.battery_max_w = int(entry_data.get(CONF_BATTERY_MAX_W, DEFAULT_BATTERY_MAX_W))
            if self.battery_max_w <= 0:
                self.battery_max_w = DEFAULT_BATTERY_MAX_W
        except Exception:
            self.battery_max_w = DEFAULT_BATTERY_MAX_W

        _LOGGER.debug("Config: battery_max_w=%s (W -> %% base)", self.battery_max_w)

        # Evita que lectura y escritura Modbus se pisen
        self._modbus_mutex = threading.Lock()

        # Evita re-ejecutar el mismo comando repetidamente
        self._last_command_signature: Optional[Tuple[str, int]] = None

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

    def _write_regs_fc16_robust(self, address, values):
        """Escritura FC16 robusta (device_id/slave/unit)."""
        values_u16 = [int(v) & 0xFFFF for v in values]
        try:
            return self.client.write_registers(address, values_u16, device_id=self.modbus_id)
        except TypeError:
            pass
        try:
            return self.client.write_registers(address, values_u16, slave=self.modbus_id)
        except TypeError:
            pass
        return self.client.write_registers(address, values_u16, unit=self.modbus_id)

    # ============================================================
    # TELEMETRÍA (SIN CAMBIOS DE LÓGICA)
    # ============================================================
    def _read_modbus_sync(self) -> Dict[str, Any]:
        """Lectura síncrona de registros Modbus."""
        with self._modbus_mutex:
            if not self.client.connect():
                raise ConnectionError(
                    f"No se pudo establecer conexión TCP con {self.entry_data[CONF_INVERTER_IP]}"
                )

            # 1. Potencia Nominal
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

            # PV Power
            ir_pv = self._read_robust(self.client.read_input_registers, 3001, 2)
            if ir_pv.isError():
                raise ModbusException("Error leyendo PV Power (3001)")
            pv_w = self._decode_u32_be(ir_pv.registers) * 0.1

            # Grid Power (3048) corregido
            ir_grid = self._read_robust(self.client.read_input_registers, 3048, 1)
            if ir_grid.isError():
                raise ModbusException("Error leyendo Grid Power (3048)")
            grid_raw = self._decode_s16(ir_grid.registers[0]) * 0.1
            grid_w = grid_raw * -1 * math.sqrt(3)

            # SOC
            ir_soc = self._read_robust(self.client.read_input_registers, 3010, 1)
            soc = ir_soc.registers[0] if not ir_soc.isError() else 0
            if soc == 0:
                ir_soc_bms = self._read_robust(self.client.read_input_registers, 3171, 1)
                if not ir_soc_bms.isError():
                    soc = ir_soc_bms.registers[0]

            # Battery power (correcto)
            ir_pdis = self._read_robust(self.client.read_input_registers, 3178, 2)
            ir_pch = self._read_robust(self.client.read_input_registers, 3180, 2)
            if ir_pdis.isError() or ir_pch.isError():
                raise ModbusException("Error leyendo potencia de batería (3178/3180)")
            pdis_w = self._decode_u32_be(ir_pdis.registers) * 0.1
            pch_w = self._decode_u32_be(ir_pch.registers) * 0.1
            bat_w = pch_w - pdis_w  # + carga / - descarga

            # Load = Grid + PV - Battery
            load_w = grid_w + pv_w - bat_w

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
                "total_grid_output_energy": to_int_str_or_none(data.get("supply_power")),
            }

            spock_response = await self._send_to_spock(spock_payload)
            await self._maybe_apply_spock_action(spock_response)

            return data

        except (ModbusException, ConnectionError) as err:
            raise UpdateFailed(f"Error de comunicación Modbus: {err}")
        except Exception as err:
            raise UpdateFailed(f"Error inesperado: {err}")

    async def _send_to_spock(self, payload) -> Optional[Dict[str, Any]]:
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
                    return None

                try:
                    data = await resp.json(content_type=None)
                    _LOGGER.debug("Respuesta de Spock: %s", data)
                    return data
                except Exception:
                    _LOGGER.warning("Spock respondió 200 OK pero no es JSON válido: %s", response_text)
                    return None

        except ClientError as err:
            _LOGGER.error("Error de conexión con Spock API: %s", err)
            return None

    # ============================================================
    # CONTROL (acciones)
    # ============================================================
    def _parse_action_w(self, raw_action: Any) -> int:
        if raw_action is None:
            return 0
        if isinstance(raw_action, (int, float)):
            return int(round(raw_action))
        if isinstance(raw_action, str):
            s = raw_action.strip().lower()
            if s in ("", "none", "null", "nan"):
                return 0
            try:
                return int(round(float(s)))
            except ValueError:
                return 0
        return 0

    async def _maybe_apply_spock_action(self, spock_response: Optional[Dict[str, Any]]) -> None:
        if not isinstance(spock_response, dict):
            _LOGGER.debug("Spock: sin JSON válido, no se aplica ninguna acción.")
            return

        status = str(spock_response.get("status", "")).strip().lower()
        if status and status != "ok":
            _LOGGER.debug("Spock: status=%s (no ok), no se aplica ninguna acción.", status)
            return

        operation_mode = str(spock_response.get("operation_mode", "none")).strip().lower()
        action_w = self._parse_action_w(spock_response.get("action"))

        if action_w == 0 and operation_mode in ("charge", "discharge"):
            desired_mode = "load_first"
            desired_value_w = 0
        else:
            if operation_mode == "charge":
                desired_mode = "charge_grid_batfirst"
                desired_value_w = action_w
            elif operation_mode == "discharge":
                desired_mode = "load_first"
                desired_value_w = action_w
            elif operation_mode in ("auto", "none"):
                desired_mode = "load_first"
                desired_value_w = 5000
            else:
                desired_mode = "load_first"
                desired_value_w = 5000

        signature = (desired_mode, int(desired_value_w))
        if self._last_command_signature == signature:
            _LOGGER.debug("Control: comando sin cambios (%s), se omite.", signature)
            return

        _LOGGER.debug(
            "Control: decisión desde Spock operation_mode=%s action=%sW -> %s (battery_max_w=%s)",
            operation_mode,
            action_w,
            signature,
            self.battery_max_w,
        )

        try:
            await self.hass.async_add_executor_job(
                self._apply_control_sync, desired_mode, int(desired_value_w)
            )
            self._last_command_signature = signature
            _LOGGER.debug("Control: aplicado OK %s", signature)
        except Exception as err:
            _LOGGER.error("Control: fallo aplicando %s: %s", signature, err)

    # -------------------------
    # Helpers control (SYNC)
    # -------------------------
    def _hr_read_u16(self, reg: int) -> int:
        r = self._read_robust(self.client.read_holding_registers, reg, 1)
        if r is None or r.isError():
            raise RuntimeError(f"Error leyendo HR{reg}: {r}")
        return int(r.registers[0])

    def _ir_read_u16(self, reg: int) -> int:
        r = self._read_robust(self.client.read_input_registers, reg, 1)
        if r is None or r.isError():
            return 0
        return int(r.registers[0])

    def _ir_read_u32_be(self, reg_h: int) -> int:
        r = self._read_robust(self.client.read_input_registers, reg_h, 2)
        if r is None or r.isError():
            return 0
        return int(self._decode_u32_be(r.registers))

    def _hr_write_u16_fc16(self, reg: int, value: int) -> None:
        r = self._write_regs_fc16_robust(reg, [int(value) & 0xFFFF])
        if r is None or r.isError():
            raise RuntimeError(f"Error escribiendo HR{reg}={hex(int(value)&0xFFFF)}: {r}")

    def _hr_write_pair_fc16(self, reg_start: int, v0: int, v1: int) -> None:
        r = self._write_regs_fc16_robust(
            reg_start, [int(v0) & 0xFFFF, int(v1) & 0xFFFF]
        )
        if r is None or r.isError():
            raise RuntimeError(f"Error escribiendo HR{reg_start}..HR{reg_start+1}: {r}")

    def _readback_until(self, reg: int, expected: int, label: str) -> bool:
        last = None
        for _ in range(_READBACK_RETRIES):
            last = self._hr_read_u16(reg)
            if last == expected:
                return True
            time.sleep(_READBACK_SLEEP_S)
        _LOGGER.debug(
            "Control: readback NO OK %s: HR%s esperado=%s leído=%s",
            label,
            reg,
            hex(expected),
            hex(last) if last is not None else None,
        )
        return False

    def _battery_snapshot(self) -> Dict[str, Any]:
        bdc = self._ir_read_u16(_IR_BDC_STATE)
        vbat_raw = self._ir_read_u16(_IR_VBAT)
        soc_raw = self._ir_read_u16(_IR_SOC)
        pdis_raw = self._ir_read_u32_be(_IR_PDIS_H)
        pch_raw = self._ir_read_u32_be(_IR_PCH_H)
        return {
            "bdc": bdc,
            "vbat_raw": vbat_raw,
            "soc_raw": soc_raw,
            "vbat": vbat_raw * 0.01,
            "pch": pch_raw * 0.1,
            "pdis": pdis_raw * 0.1,
            "net": (pch_raw - pdis_raw) * 0.1,
        }

    def _battery_online(self, s: Dict[str, Any]) -> bool:
        if (
            s.get("bdc", 0) == 0
            and s.get("vbat_raw", 0) == 0
            and s.get("soc_raw", 0) == 0
            and s.get("pch", 0) == 0
            and s.get("pdis", 0) == 0
        ):
            return False
        return (s.get("vbat_raw", 0) > 0) or (s.get("soc_raw", 0) > 0)

    def _clamp(self, v: int, lo: int, hi: int) -> int:
        return max(lo, min(hi, v))

    def _watts_to_percent(self, watts: int, base_w: int, allow_zero: bool) -> int:
        if base_w <= 0:
            return 0
        if watts <= 0:
            return 0 if allow_zero else 1
        pct = int(round((float(watts) / float(base_w)) * 100.0))
        return self._clamp(pct, 0 if allow_zero else 1, 100)

    def _encode_hm(self, h: int, m: int) -> int:
        h = max(0, min(23, int(h)))
        m = max(0, min(59, int(m)))
        return (h << 8) | m

    def _decode_time_start_from_cfg(self, cfg: int):
        sh = (cfg >> 8) & 0x1F
        sm = cfg & 0xFF
        return sh, sm

    def _decode_time_from_u16(self, v: int):
        h = (v >> 8) & 0xFF
        m = v & 0xFF
        return h, m

    def _decode_mode(self, cfg: int):
        enabled = bool(cfg & _ENABLE_BIT)
        mb = cfg & _MODE_MASK
        if mb == _MODE_LOAD:
            return enabled, "Load First"
        if mb == _MODE_BAT:
            return enabled, "Battery First"
        if mb == _MODE_GRID:
            return enabled, "Grid First"
        return enabled, "Unknown"

    def _apply_tou_time1_mode_24h(self, mode_bits: int) -> None:
        t1_cfg0 = self._hr_read_u16(_T1_CFG)
        t1_end0 = self._hr_read_u16(_T1_END)
        t2_cfg0 = self._hr_read_u16(_T2_CFG)
        t3_cfg0 = self._hr_read_u16(_T3_CFG)

        en, mode_txt = self._decode_mode(t1_cfg0)
        sh, sm = self._decode_time_start_from_cfg(t1_cfg0)
        eh, em = self._decode_time_from_u16(t1_end0)

        _LOGGER.debug(
            "Control: TOU previo: T1 cfg=%s enabled=%s mode=%s start=%02d:%02d | T1 end=%s end=%02d:%02d | T2 enabled=%s | T3 enabled=%s",
            hex(t1_cfg0), en, mode_txt, sh, sm,
            hex(t1_end0), eh, em,
            bool(t2_cfg0 & _ENABLE_BIT),
            bool(t3_cfg0 & _ENABLE_BIT),
        )

        if t2_cfg0 & _ENABLE_BIT:
            new_t2 = t2_cfg0 & ~_ENABLE_BIT
            _LOGGER.debug("Control: Disable Time2: HR%s %s -> %s", _T2_CFG, hex(t2_cfg0), hex(new_t2))
            self._hr_write_u16_fc16(_T2_CFG, new_t2)
            time.sleep(_DELAY_AFTER_WRITE_S)
            self._readback_until(_T2_CFG, new_t2, "Disable Time2")

        if t3_cfg0 & _ENABLE_BIT:
            new_t3 = t3_cfg0 & ~_ENABLE_BIT
            _LOGGER.debug("Control: Disable Time3: HR%s %s -> %s", _T3_CFG, hex(t3_cfg0), hex(new_t3))
            self._hr_write_u16_fc16(_T3_CFG, new_t3)
            time.sleep(_DELAY_AFTER_WRITE_S)
            self._readback_until(_T3_CFG, new_t3, "Disable Time3")

        start_bits = self._encode_hm(0, 0) & _TIME_BITS_MASK
        end_bits = self._encode_hm(23, 59)

        new_t1_cfg = (t1_cfg0 & ~(_MODE_MASK | _TIME_BITS_MASK)) | mode_bits | _ENABLE_BIT | start_bits
        new_t1_end = end_bits

        _LOGGER.debug(
            "Control: Forzando Time1: HR%s %s -> %s | HR%s %s -> %s",
            _T1_CFG, hex(t1_cfg0), hex(new_t1_cfg),
            _T1_END, hex(t1_end0), hex(new_t1_end),
        )

        self._hr_write_pair_fc16(_T1_CFG, new_t1_cfg, new_t1_end)
        time.sleep(_DELAY_AFTER_WRITE_S)
        self._readback_until(_T1_CFG, new_t1_cfg, "Time1 cfg")
        self._readback_until(_T1_END, new_t1_end, "Time1 end")

    def _rollback_best_effort(self, backup: Dict[str, Any]) -> None:
        try:
            self._hr_write_u16_fc16(_HR_GRID_CHARGE_ENABLE, backup["hr3049"])
            time.sleep(_DELAY_AFTER_WRITE_S)
        except Exception:
            pass

        try:
            self._hr_write_u16_fc16(_HR_CHARGE_POWER_RATE, backup["hr3047"])
            time.sleep(_DELAY_AFTER_WRITE_S)
        except Exception:
            pass

        try:
            self._hr_write_pair_fc16(_T1_CFG, backup["t1_cfg"], backup["t1_end"])
            time.sleep(_DELAY_AFTER_WRITE_S)
        except Exception:
            pass

        try:
            self._hr_write_u16_fc16(_T2_CFG, backup["t2_cfg"])
            time.sleep(_DELAY_AFTER_WRITE_S)
        except Exception:
            pass

        try:
            self._hr_write_u16_fc16(_T3_CFG, backup["t3_cfg"])
            time.sleep(_DELAY_AFTER_WRITE_S)
        except Exception:
            pass

        if backup.get("discharge_rate") is not None:
            try:
                self._hr_write_u16_fc16(_HR_DISCHARGE_POWER_RATE, backup["discharge_rate"])
                time.sleep(_DELAY_AFTER_WRITE_S)
            except Exception:
                pass

        _LOGGER.debug("Control: rollback enviado (best-effort).")

    def _apply_charge_grid_batfirst_w(self, target_charge_w: int) -> None:
        self._apply_tou_time1_mode_24h(_MODE_BAT)

        target_3049 = 1
        _LOGGER.debug("Control: HR%s GridCharging -> %s", _HR_GRID_CHARGE_ENABLE, target_3049)
        self._hr_write_u16_fc16(_HR_GRID_CHARGE_ENABLE, target_3049)
        time.sleep(_DELAY_AFTER_WRITE_S)
        self._readback_until(_HR_GRID_CHARGE_ENABLE, target_3049, "HR3049")

        pct = self._watts_to_percent(int(target_charge_w), self.battery_max_w, allow_zero=False)
        _LOGGER.debug(
            "Control: HR%s ChargeRate target=%sW base=%sW -> %s%%",
            _HR_CHARGE_POWER_RATE, target_charge_w, self.battery_max_w, pct
        )
        self._hr_write_u16_fc16(_HR_CHARGE_POWER_RATE, pct)
        time.sleep(_DELAY_AFTER_WRITE_S)
        self._readback_until(_HR_CHARGE_POWER_RATE, pct, "HR3047")

    def _apply_load_first_discharge_limit_w(self, discharge_limit_w: int) -> None:
        self._apply_tou_time1_mode_24h(_MODE_LOAD)

        pct = self._watts_to_percent(int(discharge_limit_w), self.battery_max_w, allow_zero=True)
        old = self._hr_read_u16(_HR_DISCHARGE_POWER_RATE)

        _LOGGER.debug(
            "Control: HR%s DischargeRate actual=%s%% objetivo=%sW base=%sW -> %s%%",
            _HR_DISCHARGE_POWER_RATE, old, discharge_limit_w, self.battery_max_w, pct
        )

        if old != pct:
            self._hr_write_u16_fc16(_HR_DISCHARGE_POWER_RATE, pct)
            time.sleep(_DELAY_AFTER_WRITE_S)
            ok = self._readback_until(_HR_DISCHARGE_POWER_RATE, pct, "Discharging Power Rate")
            if not ok:
                _LOGGER.debug("Control: readback fallo DischargeRate. Rollback al valor previo=%s%%", old)
                self._hr_write_u16_fc16(_HR_DISCHARGE_POWER_RATE, old)
                time.sleep(_DELAY_AFTER_WRITE_S)
                self._readback_until(_HR_DISCHARGE_POWER_RATE, old, "Rollback DischargeRate")
                raise RuntimeError("DischargeRate readback failed")

        target_3049 = 0
        _LOGGER.debug("Control: HR%s GridCharging -> %s", _HR_GRID_CHARGE_ENABLE, target_3049)
        self._hr_write_u16_fc16(_HR_GRID_CHARGE_ENABLE, target_3049)
        time.sleep(_DELAY_AFTER_WRITE_S)
        self._readback_until(_HR_GRID_CHARGE_ENABLE, target_3049, "HR3049")

    def _apply_control_sync(self, desired_mode: str, desired_value_w: int) -> None:
        with self._modbus_mutex:
            if not self.client.connect():
                raise ConnectionError(
                    f"No se pudo establecer conexión TCP con {self.entry_data[CONF_INVERTER_IP]}"
                )

            backup: Dict[str, Any] = {
                "hr3049": self._hr_read_u16(_HR_GRID_CHARGE_ENABLE),
                "hr3047": self._hr_read_u16(_HR_CHARGE_POWER_RATE),
                "t1_cfg": self._hr_read_u16(_T1_CFG),
                "t1_end": self._hr_read_u16(_T1_END),
                "t2_cfg": self._hr_read_u16(_T2_CFG),
                "t3_cfg": self._hr_read_u16(_T3_CFG),
            }
            try:
                backup["discharge_rate"] = self._hr_read_u16(_HR_DISCHARGE_POWER_RATE)
            except Exception:
                backup["discharge_rate"] = None

            s0 = self._battery_snapshot()
            _LOGGER.debug(
                "Control: snapshot previo bat: bdc=%s soc=%s vbat=%.2fV net=%.1fW",
                s0["bdc"], s0["soc_raw"], s0["vbat"], s0["net"]
            )

            if not self._battery_online(s0):
                raise RuntimeError("Battery offline BEFORE applying control")

            try:
                if desired_mode == "charge_grid_batfirst":
                    self._apply_charge_grid_batfirst_w(desired_value_w)
                elif desired_mode == "load_first":
                    self._apply_load_first_discharge_limit_w(desired_value_w)
                else:
                    raise ValueError(f"Modo no soportado: {desired_mode}")

                s_after = self._battery_snapshot()
                _LOGGER.debug(
                    "Control: snapshot final bat: bdc=%s soc=%s vbat=%.2fV net=%.1fW",
                    s_after["bdc"], s_after["soc_raw"], s_after["vbat"], s_after["net"]
                )

                if not self._battery_online(s_after):
                    raise RuntimeError("Battery offline AFTER applying control")

            except Exception as err:
                _LOGGER.error("Control: error durante apply (%s). Rollback best-effort...", err)
                self._rollback_best_effort(backup)
                raise
