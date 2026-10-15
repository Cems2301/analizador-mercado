# Analizador cuantitativo para cuenta pequeña

Busca entradas de **compra en retroceso dentro de tendencia**, calcula stop, objetivo y tamaño
de posición descontando comisiones, y hace un backtest de 3 años de cada activo.

Estrategia por defecto (`sma100-rsi7-2.5r`): Precio > SMA100 y RSI7 < 35; stop 2.0 × ATR14;
objetivo 2.5R. Se eligió con `--comparar` entre 9 variantes (ver `config.ESTRATEGIAS`) y se puede
cambiar con la variable `ESTRATEGIA` o `--estrategia`.

**Veto estadístico:** una señal técnica solo se marca como `Señal APROBADA (E>0)` si el backtest de
ese activo tiene al menos 10 operaciones y esperanza neta positiva; si no, sale como
`Señal Técnica (Vetada por E<=0 / N<10)` y no se recomienda operar.

## Fuentes de datos

| Activo | Fuente |
|---|---|
| Cripto (`BTC-USD`, `ETH-USD`, `PAXG-USD`…) | OKX → Binance → Yahoo (API públicas, sin API key) |
| Acciones/ETFs (`SPY`, `QQQ`, `DBA`…) | Yahoo Finance (`yfinance`) |

Si un exchange tiene menos de 3 años de historial de un activo, se usa la siguiente fuente con más historial.

## Uso local

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/Mac: source .venv/bin/activate)
pip install -r requirements.txt

python analizador.py                       # modo BINANCE_OKX (por defecto)
python analizador.py --modo HAPI           # SPY, QQQ, DBA con comisión fija $0.30
python analizador.py SOL-USD --intervalo 4h --capital 200
python analizador.py --comparar --intervalo 4h   # compara todas las estrategias candidatas
```

## Ejecución en la nube (GitHub Actions)

El workflow `.github/workflows/ejecucion_nube.yml` corre de lunes a viernes cada 4 horas (UTC)
y se puede lanzar a mano desde **Actions → Análisis de mercado en la nube → Run workflow**.

1. **Settings → Secrets and variables → Actions → Secrets**: crea `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.
2. Opcional, en la pestaña **Variables**: `MODO_PLATAFORMA`, `CAPITAL_TOTAL`, `ACTIVOS`
   (ej. `BTC-USD,SOL-USD`), `INTERVALO` (`1d`/`4h`), `FUENTES_CRIPTO` (ej. `binance,okx,yahoo`),
   `ESTRATEGIA`, `TELEGRAM_SOLO_CON_SENAL` (`true` para avisar solo cuando hay una señal aprobada).

En repositorios públicos, GitHub pausa los workflows programados tras 60 días sin actividad.

Todos los parámetros están en `config.py` y se pueden sobrescribir con variables de entorno.
