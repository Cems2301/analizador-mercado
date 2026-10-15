"""
Configuración central del sistema.

Cada parámetro se lee primero desde una variable de entorno (útil en GitHub
Actions o para cambiar valores sin tocar el código) y, si no existe o está
vacía, usa el valor por defecto definido aquí.

Ejemplo local (PowerShell):   $env:MODO_PLATAFORMA="HAPI"; python analizador.py
Ejemplo local (bash):         MODO_PLATAFORMA=HAPI python analizador.py
"""

import os
from dataclasses import dataclass


def _env_str(nombre: str, defecto: str) -> str:
    valor = os.getenv(nombre, "").strip()
    return valor if valor else defecto


def _env_float(nombre: str, defecto: float) -> float:
    valor = os.getenv(nombre, "").strip()
    if not valor:
        return defecto
    try:
        return float(valor.replace(",", "."))
    except ValueError:
        print(f"[config] Valor inválido en {nombre}={valor!r}; uso {defecto}")
        return defecto


def _env_bool(nombre: str, defecto: bool) -> bool:
    valor = os.getenv(nombre, "").strip().lower()
    if not valor:
        return defecto
    return valor in ("1", "true", "si", "sí", "yes", "y")


def _env_lista(nombre: str) -> list[str]:
    valor = os.getenv(nombre, "").strip()
    return [t.strip().upper() for t in valor.split(",") if t.strip()]


# --------------------------------------------------------------------------
# Gestión de capital y riesgo
# --------------------------------------------------------------------------
CAPITAL_TOTAL = _env_float("CAPITAL_TOTAL", 150.0)                # USD
RIESGO_POR_OPERACION = _env_float("RIESGO_POR_OPERACION", 0.02)   # 2% = 1R
TOPE_MAX_ASIGNACION = _env_float("TOPE_MAX_ASIGNACION", 0.50)     # máx. 50% del capital por posición
# El ratio beneficio/riesgo (objetivo en R) ahora es parte de cada estrategia: ver más abajo.

# --------------------------------------------------------------------------
# Plataforma y activos
# --------------------------------------------------------------------------
PLATAFORMAS_VALIDAS = ("BINANCE_OKX", "HAPI")

MODO_PLATAFORMA = _env_str("MODO_PLATAFORMA", "BINANCE_OKX").upper()
if MODO_PLATAFORMA not in PLATAFORMAS_VALIDAS:
    print(f"[config] MODO_PLATAFORMA={MODO_PLATAFORMA!r} no es válido; uso BINANCE_OKX")
    MODO_PLATAFORMA = "BINANCE_OKX"

ACTIVOS_POR_PLATAFORMA = {
    "BINANCE_OKX": ["BTC-USD", "ETH-USD", "PAXG-USD"],
    "HAPI": ["SPY", "QQQ", "DBA"],
}

# Si defines ACTIVOS="SOL-USD,BTC-USD" en el entorno, reemplaza la lista por defecto.
ACTIVOS_PERSONALIZADOS = _env_lista("ACTIVOS")


def activos_para(modo: str) -> list[str]:
    """Lista de tickers a analizar para la plataforma indicada."""
    return ACTIVOS_PERSONALIZADOS or ACTIVOS_POR_PLATAFORMA[modo]


# --------------------------------------------------------------------------
# Fuentes de datos
# --------------------------------------------------------------------------
# Cripto (tickers BASE-USD) se descarga directo del exchange (par BASE-USDT),
# probando en este orden hasta que una fuente responda. Acciones/ETFs: Yahoo.
# Ejemplo para cambiar el orden: FUENTES_CRIPTO="binance,okx,yahoo"
FUENTES_DISPONIBLES = ("okx", "binance", "yahoo")
FUENTES_CRIPTO = [
    f.lower() for f in _env_lista("FUENTES_CRIPTO") if f.lower() in FUENTES_DISPONIBLES
] or ["okx", "binance", "yahoo"]


# --------------------------------------------------------------------------
# Fricción (comisiones)
# --------------------------------------------------------------------------
COMISION_FIJA_HAPI = 0.30                # USD totales: compra + venta (fracciones)
COMISION_PCT_BINANCE_OKX = 0.002         # 0.20% ida y vuelta sobre el monto comprado
UMBRAL_ALERTA_COMISION = 0.10            # avisar si la comisión supera el 10% de 1R

# --------------------------------------------------------------------------
# Datos e indicadores
# --------------------------------------------------------------------------
INTERVALO = _env_str("INTERVALO", "1d").lower()   # "1d" o "4h"
if INTERVALO not in ("1d", "4h"):
    print(f"[config] INTERVALO={INTERVALO!r} no soportado; uso 1d")
    INTERVALO = "1d"

ANIOS_HISTORICO = 3
PERIODO_ATR = 14


# --------------------------------------------------------------------------
# Estrategias: compra en retroceso dentro de tendencia
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Estrategia:
    nombre: str
    periodo_sma: int        # filtro de tendencia: Precio > SMA
    periodo_rsi: int        # retroceso: RSI < umbral
    rsi_umbral: float
    rsi_giro: bool          # exigir además que el RSI suba respecto a la vela anterior
    mult_atr_stop: float    # stop = entrada - mult * ATR14
    ratio: float            # objetivo = entrada + ratio * (entrada - stop)

    def describir(self) -> str:
        giro = " y girando al alza" if self.rsi_giro else ""
        return (f"Precio > SMA{self.periodo_sma}, RSI{self.periodo_rsi} < {self.rsi_umbral:g}{giro}; "
                f"stop {self.mult_atr_stop:g} x ATR{PERIODO_ATR}, objetivo {self.ratio:g}R")


ESTRATEGIA_ORIGINAL = Estrategia("original", 50, 14, 45.0, False, 1.5, 2.5)


def _variantes():
    """Tendencia más amplia + retroceso táctico + stop más holgado (2.0 ATR)."""
    for sma in (100, 200):
        for periodo_rsi, umbral, giro in ((7, 35.0, False), (14, 50.0, True)):
            for ratio in (2.0, 2.5):
                retroceso = f"rsi{periodo_rsi}{'giro' if giro else ''}"
                yield Estrategia(f"sma{sma}-{retroceso}-{ratio:g}r", sma, periodo_rsi, umbral, giro, 2.0, ratio)


# Todas se comparan con: python analizador.py --comparar
ESTRATEGIAS = {e.nombre: e for e in (ESTRATEGIA_ORIGINAL, *_variantes())}

# Ganadora del backtest comparativo del 15-oct-2026 (9 combinaciones activo-intervalo):
# mayor P&L neto total, más operaciones que la original y E > 0 en ambas mitades del periodo.
ESTRATEGIA_POR_DEFECTO = "sma100-rsi7-2.5r"

_nombre_estrategia = _env_str("ESTRATEGIA", ESTRATEGIA_POR_DEFECTO).lower()
if _nombre_estrategia not in ESTRATEGIAS:
    print(f"[config] ESTRATEGIA={_nombre_estrategia!r} no existe; uso {ESTRATEGIA_POR_DEFECTO!r}. "
          f"Opciones: {', '.join(ESTRATEGIAS)}")
    _nombre_estrategia = ESTRATEGIA_POR_DEFECTO
ESTRATEGIA_PRINCIPAL = ESTRATEGIAS[_nombre_estrategia]

# Veto estadístico: una señal técnica solo se aprueba si el backtest del activo
# tiene al menos este número de operaciones cerradas y esperanza neta E > 0.
MIN_OPERACIONES_VALIDAS = int(_env_float("MIN_OPERACIONES_VALIDAS", 10))

# --------------------------------------------------------------------------
# Telegram (opcional)
# --------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = _env_str("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _env_str("TELEGRAM_CHAT_ID", "")
# True = solo manda mensaje cuando algún activo tiene señal activa (evita ruido cada 4h)
TELEGRAM_SOLO_CON_SENAL = _env_bool("TELEGRAM_SOLO_CON_SENAL", False)
