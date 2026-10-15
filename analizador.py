"""
Analizador cuantitativo para cuenta pequeña.

Estrategia: compra en retroceso dentro de tendencia alcista.
    Entrada : Precio > SMA50  y  RSI14 < 45
    Stop    : entrada - 1.5 * ATR14
    Objetivo: entrada + 2.5 * (entrada - stop)

Uso:
    python analizador.py                          # usa config.py / variables de entorno
    python analizador.py --modo HAPI              # SPY, QQQ, DBA con comisión fija
    python analizador.py SOL-USD BTC-USD --intervalo 4h --capital 200
"""

import argparse
import html
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests
import yfinance as yf

import config

COLUMNAS_OHLC = ["Open", "High", "Low", "Close"]


# ==========================================================================
# Descarga de datos
# ==========================================================================
class ErrorFuente(Exception):
    """Una fuente de datos no pudo entregar velas o precio."""


def es_cripto(ticker: str) -> bool:
    """Cripto en formato Yahoo: BASE-USD (BTC-USD, ETH-USD, PAXG-USD...)."""
    return ticker.upper().endswith("-USD")


def _base_cripto(ticker: str) -> str:
    return ticker.upper()[:-len("-USD")]


def _inicio_historico() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=365 * config.ANIOS_HISTORICO)


def _limpiar_velas(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    df = df[COLUMNAS_OHLC + ["Volume"]].dropna(subset=COLUMNAS_OHLC)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if len(df) < config.PERIODO_SMA + 10:
        raise ErrorFuente(f"historial insuficiente para {ticker}: {len(df)} velas")
    return df


def _filas_a_velas(filas: list, ticker: str) -> pd.DataFrame:
    """Convierte filas de exchange [ms, open, high, low, close, volumen, ...] en velas."""
    df = pd.DataFrame([f[:6] for f in filas], columns=["ts"] + COLUMNAS_OHLC + ["Volume"])
    df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("ts").astype("int64"), unit="ms", utc=True))
    df = df.astype(float)
    return _limpiar_velas(df[df.index >= _inicio_historico()], ticker)


def _get_json(url: str, params: dict, intentos: int = 3):
    """GET con reintentos solo ante errores transitorios (red, 429, 5xx)."""
    error = "sin respuesta"
    for intento in range(1, intentos + 1):
        try:
            respuesta = requests.get(url, params=params, timeout=15)
        except requests.RequestException as e:
            error = f"conexión fallida ({type(e).__name__})"
        else:
            if respuesta.status_code == 200:
                return respuesta.json()
            error = f"HTTP {respuesta.status_code}: {' '.join(respuesta.text.split())[:90]}"
            if respuesta.status_code not in (429, 500, 502, 503, 504):
                break  # p. ej. 451 = bloqueo geográfico: reintentar no sirve
        if intento < intentos:
            time.sleep(2 * intento)
    raise ErrorFuente(error)


# --- OKX (API pública, sin API key) ---------------------------------------
OKX_API = "https://www.okx.com/api/v5/market"


def _okx(endpoint: str, params: dict) -> list:
    respuesta = _get_json(f"{OKX_API}/{endpoint}", params)
    if respuesta.get("code") != "0":
        raise ErrorFuente(f"OKX {respuesta.get('code')}: {respuesta.get('msg')}")
    return respuesta["data"]


def velas_okx(ticker: str, intervalo: str) -> tuple[pd.DataFrame, str]:
    instrumento = f"{_base_cripto(ticker)}-USDT"
    barra = {"1d": "1Dutc", "4h": "4H"}[intervalo]
    limite_ms = _inicio_historico().timestamp() * 1000
    filas, despues = [], None
    for _ in range(60):
        params = {"instId": instrumento, "bar": barra, "limit": "300"}
        if despues:
            params["after"] = despues  # velas anteriores a este instante
        pagina = _okx("history-candles", params)  # de la más reciente a la más antigua
        if not pagina:
            break
        filas.extend(pagina)
        despues = pagina[-1][0]
        if int(despues) <= limite_ms:
            break
        time.sleep(0.15)  # límite de OKX: 20 peticiones cada 2 s
    if not filas:
        raise ErrorFuente(f"OKX sin velas para {instrumento}")
    return _filas_a_velas(filas, ticker), f"OKX {instrumento}"


def precio_okx(ticker: str) -> tuple[float, pd.Timestamp]:
    dato = _okx("ticker", {"instId": f"{_base_cripto(ticker)}-USDT"})[0]
    return float(dato["last"]), pd.Timestamp(int(dato["ts"]), unit="ms", tz="UTC")


# --- Binance (API pública, sin API key) -----------------------------------
# data-api.binance.vision es el espejo público de datos de mercado; api.binance.com
# responde 451 desde EE. UU. (incluidos los servidores de GitHub Actions).
BINANCE_APIS = ("https://data-api.binance.vision/api/v3", "https://api.binance.com/api/v3")


def _binance(endpoint: str, params: dict):
    fallos = []
    for base in BINANCE_APIS:
        try:
            return _get_json(f"{base}/{endpoint}", params)
        except ErrorFuente as e:
            fallos.append(f"{base.split('/')[2]} {e}")
    raise ErrorFuente("; ".join(fallos))


def velas_binance(ticker: str, intervalo: str) -> tuple[pd.DataFrame, str]:
    simbolo = f"{_base_cripto(ticker)}USDT"
    desde = int(_inicio_historico().timestamp() * 1000)
    filas = []
    for _ in range(20):
        pagina = _binance("klines", {"symbol": simbolo, "interval": intervalo,
                                     "startTime": desde, "limit": 1000})
        if not pagina:
            break
        filas.extend(pagina)
        if len(pagina) < 1000:
            break
        desde = pagina[-1][0] + 1
    if not filas:
        raise ErrorFuente(f"Binance sin velas para {simbolo}")
    return _filas_a_velas(filas, ticker), f"Binance {simbolo}"


def precio_binance(ticker: str) -> tuple[float, pd.Timestamp]:
    dato = _binance("ticker/price", {"symbol": f"{_base_cripto(ticker)}USDT"})
    return float(dato["price"]), pd.Timestamp.now(tz="UTC")


# --- Yahoo Finance (acciones/ETFs, y último recurso para cripto) ----------
def _historial(ticker: str, intentos: int = 3, **kwargs) -> pd.DataFrame:
    """Descarga velas con reintentos (Yahoo a veces corta o limita peticiones)."""
    ultimo_error = None
    for intento in range(1, intentos + 1):
        try:
            df = yf.Ticker(ticker).history(auto_adjust=True, **kwargs)
            if df is not None and not df.empty:
                return df
            ultimo_error = "respuesta vacía"
        except Exception as e:  # yfinance lanza tipos de error muy variados
            ultimo_error = e
        if intento < intentos:
            time.sleep(3 * intento)
    raise ErrorFuente(f"Yahoo sin datos de {ticker}: {ultimo_error}")


def velas_yahoo(ticker: str, intervalo: str) -> tuple[pd.DataFrame, str]:
    """Velas OHLC de los últimos 3 años (1d) o del máximo disponible (4h)."""
    if intervalo == "4h":
        # Yahoo no ofrece velas de 4h y limita las de 1h a 730 días:
        # se descargan velas de 1h y se agrupan en bloques de 4h.
        inicio = datetime.now(timezone.utc) - timedelta(days=729)
        horario = _historial(ticker, start=inicio, interval="1h")
        df = (
            horario.resample("4h", origin="start_day")
            .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
        )
    else:
        df = _historial(ticker, start=_inicio_historico(), interval="1d")
    return _limpiar_velas(df, ticker), f"Yahoo {ticker}"


def precio_yahoo(ticker: str) -> tuple[float, pd.Timestamp]:
    """Último precio negociado (vela de 1 minuto más reciente)."""
    minuto = _historial(ticker, intentos=2, period="5d", interval="1m")
    cierre = minuto["Close"].dropna()
    if cierre.empty:
        raise ErrorFuente("sin precio de 1 minuto")
    return float(cierre.iloc[-1]), cierre.index[-1]


FUENTES = {
    "okx": (velas_okx, precio_okx),
    "binance": (velas_binance, precio_binance),
    "yahoo": (velas_yahoo, precio_yahoo),
}


def obtener_datos(ticker: str, intervalo: str):
    """Velas + precio en vivo, probando fuentes en orden hasta que una responda.

    Cripto (BASE-USD): config.FUENTES_CRIPTO (por defecto OKX -> Binance -> Yahoo).
    Acciones/ETFs: Yahoo Finance.
    Si una fuente tiene historial corto (activo listado hace poco en ese exchange),
    se prueba la siguiente y se usa la de historial más largo.
    Devuelve (velas, precio, marca_tiempo, etiqueta_fuente, avisos).
    """
    fuentes = config.FUENTES_CRIPTO if es_cripto(ticker) else ["yahoo"]
    periodo_minimo = timedelta(days=365 * config.ANIOS_HISTORICO * 0.9)
    avisos, mejor = [], None
    for nombre in fuentes:
        try:
            df, etiqueta = FUENTES[nombre][0](ticker, intervalo)
        except Exception as e:
            avisos.append(f"{nombre} falló: {e}")
            continue
        if mejor is None or len(df) > len(mejor[0]):
            mejor = (df, etiqueta, nombre)
        if df.index[-1] - df.index[0] >= periodo_minimo:
            break
        avisos.append(f"{etiqueta} solo tiene historial desde {df.index[0]:%Y-%m-%d}")

    if mejor is None:
        raise ErrorFuente(" | ".join(avisos))
    df, etiqueta, nombre = mejor
    try:
        precio, marca = FUENTES[nombre][1](ticker)
    except Exception:
        precio, marca = None, None
    return df, precio, marca, etiqueta, avisos


def incorporar_precio_vivo(df: pd.DataFrame, precio: float, marca: pd.Timestamp,
                           intervalo: str) -> pd.DataFrame:
    """Actualiza la vela en curso con el precio en vivo (o abre una nueva si hace falta)."""
    df = df.copy()
    ultimo = df.index[-1]
    duracion = pd.Timedelta(hours=4) if intervalo == "4h" else pd.Timedelta(days=1)

    if marca >= ultimo + duracion:
        # El precio en vivo pertenece a una vela que Yahoo aún no publicó.
        if intervalo == "4h":
            inicio = ultimo + duracion * ((marca - ultimo) // duracion)
        else:
            inicio = marca.tz_convert(ultimo.tz).normalize()
        nueva = pd.DataFrame(
            {"Open": precio, "High": precio, "Low": precio, "Close": precio, "Volume": 0.0},
            index=pd.DatetimeIndex([inicio]),
        )
        return pd.concat([df, nueva])

    df.loc[ultimo, "Close"] = precio
    df.loc[ultimo, "High"] = max(df.loc[ultimo, "High"], precio)
    df.loc[ultimo, "Low"] = min(df.loc[ultimo, "Low"], precio)
    return df


# ==========================================================================
# Indicadores (solo pandas / numpy)
# ==========================================================================
def calcular_sma(serie: pd.Series, periodo: int) -> pd.Series:
    return serie.rolling(periodo, min_periods=periodo).mean()


def calcular_rsi(cierre: pd.Series, periodo: int) -> pd.Series:
    """RSI con suavizado de Wilder."""
    delta = cierre.diff()
    ganancia = delta.clip(lower=0.0)
    perdida = -delta.clip(upper=0.0)
    alfa = 1.0 / periodo
    media_ganancia = ganancia.ewm(alpha=alfa, adjust=False, min_periods=periodo).mean()
    media_perdida = perdida.ewm(alpha=alfa, adjust=False, min_periods=periodo).mean()
    rs = media_ganancia / media_perdida
    rsi = 100.0 - 100.0 / (1.0 + rs)
    return rsi.where(media_perdida != 0, 100.0)


def calcular_atr(df: pd.DataFrame, periodo: int) -> pd.Series:
    """Average True Range con suavizado de Wilder."""
    cierre_previo = df["Close"].shift(1)
    rango_verdadero = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - cierre_previo).abs(),
            (df["Low"] - cierre_previo).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return rango_verdadero.ewm(alpha=1.0 / periodo, adjust=False, min_periods=periodo).mean()


def agregar_indicadores(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["SMA"] = calcular_sma(df["Close"], config.PERIODO_SMA)
    df["RSI"] = calcular_rsi(df["Close"], config.PERIODO_RSI)
    df["ATR"] = calcular_atr(df, config.PERIODO_ATR)
    df["senal"] = (df["Close"] > df["SMA"]) & (df["RSI"] < config.RSI_UMBRAL_ENTRADA)
    return df


# ==========================================================================
# Fricción y tamaño de posición
# ==========================================================================
def comision_total(monto_usd: float, modo: str) -> float:
    """Comisión de ida y vuelta (compra + venta) en USD."""
    if modo == "HAPI":
        return config.COMISION_FIJA_HAPI
    return monto_usd * config.COMISION_PCT_BINANCE_OKX


def advertencia_friccion(capital: float, modo: str) -> str | None:
    """Avisa si la comisión fija de HAPI supera el 10% de 1R."""
    if modo != "HAPI":
        return None
    un_r = capital * config.RIESGO_POR_OPERACION
    comision = config.COMISION_FIJA_HAPI
    if comision > config.UMBRAL_ALERTA_COMISION * un_r:
        return (f"ADVERTENCIA: la comisión fija de ${comision:.2f} es el {comision / un_r:.0%} "
                f"de 1R (${un_r:.2f}), por encima del {config.UMBRAL_ALERTA_COMISION:.0%} recomendado. "
                f"La fricción reduce mucho la esperanza; considera más capital o menos operaciones.")
    return None


@dataclass
class PlanOperacion:
    entrada: float
    stop: float
    take_profit: float
    distancia_stop_pct: float
    monto_usd: float = 0.0
    unidades: float = 0.0
    comision: float = 0.0
    riesgo_objetivo: float = 0.0      # 1R en USD
    perdida_neta_sl: float = 0.0      # lo que se pierde realmente si salta el stop
    ganancia_neta_tp: float = 0.0     # lo que se gana realmente si toca el objetivo
    limitado_por_tope: bool = False
    valido: bool = True
    notas: list[str] = field(default_factory=list)


def calcular_plan(entrada: float, atr: float, capital: float, modo: str) -> PlanOperacion:
    """Stop, objetivo y tamaño de posición en USD descontando comisiones.

    Se dimensiona para que la pérdida total en el stop (movimiento de precio
    + comisión) sea exactamente 1R, salvo que el tope de asignación lo impida.
    """
    riesgo_unitario = config.MULTIPLICADOR_ATR_STOP * atr
    stop = entrada - riesgo_unitario
    plan = PlanOperacion(
        entrada=entrada,
        stop=stop,
        take_profit=entrada + config.RATIO_BENEFICIO_RIESGO * riesgo_unitario,
        distancia_stop_pct=riesgo_unitario / entrada if entrada > 0 else float("nan"),
        riesgo_objetivo=capital * config.RIESGO_POR_OPERACION,
    )
    if not (math.isfinite(atr) and atr > 0 and stop > 0):
        plan.valido = False
        plan.notas.append("ATR inválido o stop por debajo de cero")
        return plan

    d = plan.distancia_stop_pct
    un_r = plan.riesgo_objetivo
    if modo == "HAPI":
        # pérdida = monto * d + comisión_fija  =>  monto = (1R - comisión) / d
        disponible = un_r - config.COMISION_FIJA_HAPI
        if disponible <= 0:
            plan.valido = False
            plan.notas.append(f"1R (${un_r:.2f}) no alcanza a cubrir la comisión fija")
            return plan
        monto = disponible / d
    else:
        # pérdida = monto * d + monto * comisión%  =>  monto = 1R / (d + comisión%)
        monto = un_r / (d + config.COMISION_PCT_BINANCE_OKX)

    tope = capital * config.TOPE_MAX_ASIGNACION
    if monto > tope:
        monto = tope
        plan.limitado_por_tope = True

    plan.monto_usd = monto
    plan.unidades = monto / entrada
    plan.comision = comision_total(monto, modo)
    plan.perdida_neta_sl = monto * d + plan.comision
    plan.ganancia_neta_tp = monto * config.RATIO_BENEFICIO_RIESGO * d - plan.comision
    if plan.limitado_por_tope:
        plan.notas.append(
            f"Posición limitada al tope del {config.TOPE_MAX_ASIGNACION:.0%} (${tope:.2f}); "
            f"riesgo real ${plan.perdida_neta_sl:.2f} < 1R"
        )
    return plan


# ==========================================================================
# Backtest iterativo
# ==========================================================================
def backtest(df: pd.DataFrame, capital: float, modo: str) -> dict:
    """Simula la estrategia vela a vela sobre todo el historial.

    - La señal se evalúa al cierre de la vela i y se entra en la apertura de i+1.
    - Una sola posición a la vez; tamaño calculado con capital fijo (sin interés compuesto).
    - Si en la misma vela se tocan stop y objetivo, se asume el stop (conservador).
    - Si la vela abre con hueco más allá del stop/objetivo, se sale a la apertura.
    """
    apertura = df["Open"].to_numpy()
    maximo = df["High"].to_numpy()
    minimo = df["Low"].to_numpy()
    senal = df["senal"].to_numpy()
    atr = df["ATR"].to_numpy()
    fechas = df.index
    n = len(df)

    operaciones = []
    posicion_abierta = None
    i = 0
    while i < n - 1:
        if not senal[i]:
            i += 1
            continue

        j = i + 1
        entrada = apertura[j]
        plan = calcular_plan(entrada, atr[i], capital, modo)
        if not plan.valido:
            i += 1
            continue

        salida, motivo, k = None, None, j
        while k < n:
            if minimo[k] <= plan.stop:
                salida, motivo = min(apertura[k], plan.stop), "SL"
                break
            if maximo[k] >= plan.take_profit:
                salida, motivo = max(apertura[k], plan.take_profit), "TP"
                break
            k += 1

        if salida is None:
            posicion_abierta = {"fecha_entrada": fechas[j], "entrada": entrada,
                                "stop": plan.stop, "take_profit": plan.take_profit}
            break

        pnl = plan.monto_usd * (salida / entrada - 1.0) - plan.comision
        operaciones.append({
            "fecha_entrada": fechas[j], "fecha_salida": fechas[k],
            "entrada": entrada, "salida": salida, "motivo": motivo,
            "monto": plan.monto_usd, "pnl": pnl,
        })
        i = k  # la siguiente señal puede darse al cierre de la vela de salida

    return resumir_backtest(operaciones, posicion_abierta, capital)


def resumir_backtest(operaciones: list[dict], posicion_abierta: dict | None, capital: float) -> dict:
    un_r = capital * config.RIESGO_POR_OPERACION
    pnl = np.array([op["pnl"] for op in operaciones], dtype=float)
    total = len(pnl)
    resumen = {
        "operaciones": operaciones, "posicion_abierta": posicion_abierta, "total": total,
        "win_rate": 0.0, "ganancia_media": 0.0, "perdida_media": 0.0,
        "esperanza_usd": 0.0, "esperanza_r": 0.0, "pnl_total": 0.0,
        "profit_factor": float("nan"), "max_drawdown": 0.0, "max_racha_perdidas": 0,
    }
    if total == 0:
        return resumen

    ganadoras = pnl[pnl > 0]
    perdedoras = pnl[pnl <= 0]
    win_rate = len(ganadoras) / total
    ganancia_media = ganadoras.mean() if len(ganadoras) else 0.0
    perdida_media = -perdedoras.mean() if len(perdedoras) else 0.0   # en positivo
    esperanza = win_rate * ganancia_media - (1 - win_rate) * perdida_media

    curva = np.cumsum(pnl)
    drawdown = np.maximum.accumulate(np.concatenate([[0.0], curva]))[1:] - curva

    racha = max_racha = 0
    for valor in pnl:
        racha = racha + 1 if valor <= 0 else 0
        max_racha = max(max_racha, racha)

    suma_perdidas = -perdedoras.sum()
    resumen.update({
        "win_rate": win_rate,
        "ganancia_media": ganancia_media,
        "perdida_media": perdida_media,
        "esperanza_usd": esperanza,
        "esperanza_r": esperanza / un_r if un_r else float("nan"),
        "pnl_total": pnl.sum(),
        "profit_factor": ganadoras.sum() / suma_perdidas if suma_perdidas > 0 else float("inf"),
        "max_drawdown": drawdown.max(),
        "max_racha_perdidas": max_racha,
    })
    return resumen


# ==========================================================================
# Análisis por activo
# ==========================================================================
def analizar_activo(ticker: str, modo: str, capital: float, intervalo: str) -> dict:
    df, precio, marca, fuente, fallos_fuente = obtener_datos(ticker, intervalo)
    if precio is not None:
        df = incorporar_precio_vivo(df, precio, marca, intervalo)
        fuente_precio = f"en vivo ({marca.tz_convert('UTC'):%Y-%m-%d %H:%M} UTC)"
    else:
        precio = float(df["Close"].iloc[-1])
        fuente_precio = "último cierre (sin precio en vivo)"

    df = agregar_indicadores(df)
    ultima = df.iloc[-1]
    senal_activa = bool(ultima["senal"])
    plan = calcular_plan(precio, float(ultima["ATR"]), capital, modo) if senal_activa else None

    return {
        "ticker": ticker,
        "fuente": fuente,
        "fallos_fuente": fallos_fuente,
        "precio": precio,
        "fuente_precio": fuente_precio,
        "sma": float(ultima["SMA"]),
        "rsi": float(ultima["RSI"]),
        "atr": float(ultima["ATR"]),
        "senal": senal_activa,
        "plan": plan,
        "backtest": backtest(df, capital, modo),
        "desde": df.index[0],
        "hasta": df.index[-1],
        "velas": len(df),
    }


# ==========================================================================
# Reportes
# ==========================================================================
def fmt_precio(valor: float) -> str:
    if not math.isfinite(valor):
        return "n/d"
    if abs(valor) >= 100:
        return f"{valor:,.2f}"
    if abs(valor) >= 1:
        return f"{valor:,.4f}"
    return f"{valor:.6g}"


def fmt_usd(valor: float, signo: bool = False) -> str:
    texto = f"{abs(valor):,.2f}"
    if signo:
        return f"{'+' if valor >= 0 else '-'}${texto}"
    return f"{'-' if valor < 0 else ''}${texto}"


def motivo_sin_senal(r: dict) -> str:
    motivos = []
    if not r["precio"] > r["sma"]:
        motivos.append(f"precio <= SMA{config.PERIODO_SMA}")
    if not r["rsi"] < config.RSI_UMBRAL_ENTRADA:
        motivos.append(f"RSI {r['rsi']:.1f} >= {config.RSI_UMBRAL_ENTRADA:.0f}")
    return ", ".join(motivos) or "condiciones no cumplidas"


def reporte_consola(r: dict, modo: str) -> str:
    bt = r["backtest"]
    lineas = [
        "-" * 72,
        f"{r['ticker']}  |  {r['fuente']}  |  precio {fmt_precio(r['precio'])}  [{r['fuente_precio']}]",
        "-" * 72,
        *[f"  Aviso de datos: {aviso}" for aviso in r["fallos_fuente"]],
        f"  SMA{config.PERIODO_SMA}: {fmt_precio(r['sma'])}   RSI{config.PERIODO_RSI}: {r['rsi']:.2f}   "
        f"ATR{config.PERIODO_ATR}: {fmt_precio(r['atr'])} ({r['atr'] / r['precio']:.2%})",
        f"  Precio > SMA{config.PERIODO_SMA}: {'SI' if r['precio'] > r['sma'] else 'NO'}   "
        f"RSI < {config.RSI_UMBRAL_ENTRADA:.0f}: {'SI' if r['rsi'] < config.RSI_UMBRAL_ENTRADA else 'NO'}",
    ]

    if r["senal"]:
        p = r["plan"]
        lineas.append("  >>> SEÑAL DE ENTRADA ACTIVA HOY <<<")
        if p.valido:
            lineas += [
                f"  Entrada      : {fmt_precio(p.entrada)}",
                f"  Stop Loss    : {fmt_precio(p.stop)}  (-{p.distancia_stop_pct:.2%}, "
                f"{config.MULTIPLICADOR_ATR_STOP} x ATR)",
                f"  Take Profit  : {fmt_precio(p.take_profit)}  "
                f"(+{p.distancia_stop_pct * config.RATIO_BENEFICIO_RIESGO:.2%}, "
                f"{config.RATIO_BENEFICIO_RIESGO}R)",
                f"  Tamaño       : {fmt_usd(p.monto_usd)}  ({p.unidades:.8f} unidades)",
                f"  Comisión i/v : {fmt_usd(p.comision)} ({modo})",
                f"  Pérdida neta si toca SL : {fmt_usd(p.perdida_neta_sl)}  (1R = {fmt_usd(p.riesgo_objetivo)})",
                f"  Ganancia neta si toca TP: {fmt_usd(p.ganancia_neta_tp)}",
            ]
        lineas += [f"  Nota: {nota}" for nota in p.notas]
    else:
        lineas.append(f"  Sin señal hoy: {motivo_sin_senal(r)}")

    lineas += [
        "",
        f"  Backtest {r['desde']:%Y-%m-%d} -> {r['hasta']:%Y-%m-%d} ({r['velas']} velas)",
        f"    Operaciones cerradas : {bt['total']}",
    ]
    if bt["total"]:
        lineas += [
            f"    Tasa de acierto      : {bt['win_rate']:.1%}",
            f"    Ganancia neta media  : {fmt_usd(bt['ganancia_media'])}   "
            f"Pérdida neta media: {fmt_usd(bt['perdida_media'])}",
            f"    Esperanza neta (E)   : {fmt_usd(bt['esperanza_usd'], signo=True)} por operación "
            f"({bt['esperanza_r']:+.2f}R)",
            f"    P&L total            : {fmt_usd(bt['pnl_total'], signo=True)}   "
            f"Profit factor: {bt['profit_factor']:.2f}",
            f"    Máx. drawdown        : {fmt_usd(bt['max_drawdown'])}   "
            f"Máx. racha de pérdidas: {bt['max_racha_perdidas']}",
            "    Últimas operaciones:",
        ]
        for op in bt["operaciones"][-5:]:
            lineas.append(
                f"      {op['fecha_entrada']:%Y-%m-%d} -> {op['fecha_salida']:%Y-%m-%d}  "
                f"{op['motivo']}  {fmt_precio(op['entrada'])} -> {fmt_precio(op['salida'])}  "
                f"{fmt_usd(op['pnl'], signo=True)}"
            )
    if bt["posicion_abierta"]:
        ab = bt["posicion_abierta"]
        lineas.append(
            f"    Posición abierta en backtest desde {ab['fecha_entrada']:%Y-%m-%d} "
            f"(entrada {fmt_precio(ab['entrada'])}, SL {fmt_precio(ab['stop'])}, "
            f"TP {fmt_precio(ab['take_profit'])})"
        )
    return "\n".join(lineas)


def tabla_resumen(resultados: list[dict]) -> str:
    columnas = (f"{'Activo':<9} {'Fuente':<14} {'Precio':>11} {'SMA50':>11} {'RSI':>5} "
                f"{'Señal':<5} {'Ops':>4} {'Acierto':>7} {'E/op':>7} {'E(R)':>6}")
    lineas = [columnas, "-" * len(columnas)]
    for r in resultados:
        bt = r["backtest"]
        lineas.append(
            f"{r['ticker']:<9} {r['fuente']:<14} {fmt_precio(r['precio']):>11} {fmt_precio(r['sma']):>11} "
            f"{r['rsi']:>5.1f} {'SI' if r['senal'] else 'no':<5} {bt['total']:>4} "
            f"{bt['win_rate']:>7.1%} {fmt_usd(bt['esperanza_usd'], signo=True):>7} {bt['esperanza_r']:>+6.2f}"
        )
    return "\n".join(lineas)


def resumen_telegram(resultados: list[dict], errores: dict, modo: str, capital: float,
                     intervalo: str, aviso_friccion: str | None) -> str:
    ahora = datetime.now(timezone.utc)
    un_r = capital * config.RIESGO_POR_OPERACION
    lineas = [
        f"📊 {modo} | {intervalo} | {ahora:%Y-%m-%d %H:%M} UTC",
        f"Capital {fmt_usd(capital)} | 1R {fmt_usd(un_r)} | Tope {fmt_usd(capital * config.TOPE_MAX_ASIGNACION)}",
    ]
    if aviso_friccion:
        lineas.append(f"⚠️ Comisión > {config.UMBRAL_ALERTA_COMISION:.0%} de 1R")

    for r in resultados:
        bt = r["backtest"]
        lineas += ["", f"[{r['fuente']}]"]
        if r["senal"] and r["plan"].valido:
            p = r["plan"]
            lineas += [
                f"🟢 {r['ticker']} {fmt_precio(r['precio'])}  SEÑAL DE COMPRA",
                f"  SL {fmt_precio(p.stop)} | TP {fmt_precio(p.take_profit)}",
                f"  Monto {fmt_usd(p.monto_usd)} | -{fmt_usd(p.perdida_neta_sl)} / +{fmt_usd(p.ganancia_neta_tp)}",
            ]
            if p.limitado_por_tope:
                lineas.append("  (limitado por tope de asignación)")
        elif r["senal"]:
            lineas.append(f"🟡 {r['ticker']} {fmt_precio(r['precio'])}  señal sin plan válido: "
                          f"{'; '.join(r['plan'].notas)}")
        else:
            lineas.append(f"⚪ {r['ticker']} {fmt_precio(r['precio'])}  sin señal (RSI {r['rsi']:.1f})")
        if bt["total"]:
            lineas.append(
                f"  BT: {bt['total']} ops | WR {bt['win_rate']:.0%} | "
                f"E {fmt_usd(bt['esperanza_usd'], signo=True)}/op"
            )
        else:
            lineas.append("  BT: sin operaciones cerradas")

    for ticker, error in errores.items():
        lineas += ["", f"❌ {ticker}: {error}"]
    return "\n".join(lineas)


def enviar_telegram(texto: str) -> bool:
    token, chat_id = config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    trozos, actual = [], ""
    for linea in texto.splitlines():
        if len(actual) + len(linea) + 1 > 3500:
            trozos.append(actual)
            actual = ""
        actual += linea + "\n"
    if actual.strip():
        trozos.append(actual)

    try:
        for trozo in trozos:
            respuesta = requests.post(
                url,
                data={"chat_id": chat_id, "text": f"<pre>{html.escape(trozo)}</pre>", "parse_mode": "HTML"},
                timeout=20,
            )
            if not respuesta.ok:
                print(f"[telegram] Error {respuesta.status_code}: {respuesta.text[:200]}")
                return False
    except requests.RequestException as e:
        # El token viaja en la URL: se oculta antes de imprimir el error.
        print(f"[telegram] Fallo de conexión: {str(e).replace(token, '***')}")
        return False
    return True


# ==========================================================================
# Programa principal
# ==========================================================================
def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    parser = argparse.ArgumentParser(description="Análisis cuantitativo para cuenta pequeña")
    parser.add_argument("activos", nargs="*", help="Tickers de Yahoo Finance (por defecto, los de config.py)")
    parser.add_argument("--modo", choices=config.PLATAFORMAS_VALIDAS, default=config.MODO_PLATAFORMA)
    parser.add_argument("--intervalo", choices=["1d", "4h"], default=config.INTERVALO)
    parser.add_argument("--capital", type=float, default=config.CAPITAL_TOTAL)
    parser.add_argument("--sin-telegram", action="store_true", help="No enviar el resumen por Telegram")
    args = parser.parse_args()

    modo, capital, intervalo = args.modo, args.capital, args.intervalo
    activos = [a.upper() for a in args.activos] or config.activos_para(modo)
    un_r = capital * config.RIESGO_POR_OPERACION

    print("=" * 72)
    print(f"ANÁLISIS CUANTITATIVO  |  {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    print(f"Plataforma: {modo}   Intervalo: {intervalo}   Activos: {', '.join(activos)}")
    print(f"Capital: {fmt_usd(capital)}   1R ({config.RIESGO_POR_OPERACION:.0%}): {fmt_usd(un_r)}   "
          f"Tope por posición: {fmt_usd(capital * config.TOPE_MAX_ASIGNACION)}   "
          f"Objetivo: {config.RATIO_BENEFICIO_RIESGO}R")
    if modo == "HAPI":
        print(f"Comisión: {fmt_usd(config.COMISION_FIJA_HAPI)} fija ida y vuelta "
              f"({config.COMISION_FIJA_HAPI / un_r:.0%} de 1R)")
    else:
        print(f"Comisión: {config.COMISION_PCT_BINANCE_OKX:.2%} ida y vuelta sobre el monto")
    if intervalo == "4h":
        print("Nota: en 4h, Yahoo solo entrega ~730 días de historial (acciones/ETFs); "
              "OKX y Binance sí dan 3 años.")
    aviso_friccion = advertencia_friccion(capital, modo)
    if aviso_friccion:
        print(aviso_friccion)
    print("=" * 72)

    resultados, errores = [], {}
    for ticker in activos:
        try:
            resultado = analizar_activo(ticker, modo, capital, intervalo)
        except Exception as e:
            errores[ticker] = str(e)
            print("-" * 72)
            print(f"{ticker}: ERROR -> {e}")
            continue
        resultados.append(resultado)
        print(reporte_consola(resultado, modo))

    print("=" * 72)
    if resultados:
        print("RESUMEN")
        print(tabla_resumen(resultados))
        print("=" * 72)
    senales = [r["ticker"] for r in resultados if r["senal"]]
    print(f"Señales activas: {', '.join(senales) if senales else 'ninguna'}")

    if not args.sin_telegram and config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        if config.TELEGRAM_SOLO_CON_SENAL and not senales and not errores:
            print("[telegram] Sin señales: no se envía mensaje (TELEGRAM_SOLO_CON_SENAL activo).")
        else:
            texto = resumen_telegram(resultados, errores, modo, capital, intervalo, aviso_friccion)
            if enviar_telegram(texto):
                print("[telegram] Resumen enviado.")
    elif not args.sin_telegram:
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID no configurados: solo consola.")

    return 1 if not resultados else 0


if __name__ == "__main__":
    sys.exit(main())
