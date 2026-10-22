# Alerta de granizo — Arroyo Seco

`alerta_granizo.py` avisa al celular en dos tiempos:

| Etapa          | Fuente                               | Anticipación      | Cada cuánto |
|----------------|--------------------------------------|-------------------|-------------|
| **Pronóstico** | Open-Meteo (modelos GFS + ECMWF)     | horas a 1–2 días  | 1 h (cache) |
| **En vivo**    | Rayos del satélite GOES-19 (GLM, NOAA) | 10–60 minutos   | 5 min       |

### Pronóstico: índice SHIP

| SHIP   | Riesgo   | Qué significa                             |
|--------|----------|-------------------------------------------|
| ≥ 1.0  | ALTO     | ambiente para granizo grande (≥ 5 cm)     |
| ≥ 0.5  | MODERADO | granizo posible                           |
| ≥ 0.2  | BAJO     | algo de potencial (con CAPE ≥ 500 J/kg)   |

Si la probabilidad de lluvia es < 20 % no avisa; si es < 40 % baja un nivel.
Avisa una vez por día y vuelve a avisar solo si el riesgo sube.

### En vivo: rayos

Solo se descargan cuando el pronóstico muestra ambiente de tormenta (CAPE ≥ 200 o lluvia ≥ 30 %
entre 2 h antes y 3 h después), para no bajar datos al pedo. Mira los últimos 10 min:

| Situación                                         | Aviso             |
|---------------------------------------------------|-------------------|
| rayos a ≤ 15 km                                   | Tormenta encima (urgente) |
| rayos a ≤ 40 km, o salto brusco de rayos          | Tormenta cerca    |
| rayos a ≤ 100 km y el pronóstico da MODERADO/ALTO | Tormenta en la zona |

También dice para qué lado está, si se acerca (y en cuánto llegaría) o se aleja.
Un **salto brusco de rayos** (la cantidad se duplica en 5 min) es una señal típica
de que una celda se está fortaleciendo y puede tirar piedra.
Avisa cuando empeora y recuerda cada 45 min mientras siga activa.

## 1. Instalar

```bash
pip install h5py
```

(Sin `h5py` anda igual, pero solo con el pronóstico.)

## 2. Recibir los avisos en el celular (ntfy, gratis, sin cuenta)

1. Instalá la app **ntfy** (Play Store / App Store).
2. Suscribite a un tema con nombre difícil de adivinar, p. ej. `granizo-arroyoseco-x7k2`.
3. Probá:

```bash
NTFY_TOPIC=granizo-arroyoseco-x7k2 python alerta_granizo.py --test
```

Alternativa: Telegram, con `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.

## 3. Ver qué está calculando

```bash
python alerta_granizo.py --dry-run --forzar
```

## 4. Correrlo solo, cada 5 minutos

**En GitHub Actions (gratis, sin servidor)** — ya viene armado en `.github/workflows/alerta.yml`:

1. Creá un repo **público** en GitHub (en los públicos los minutos de Actions son ilimitados)
   y subí esta carpeta.
2. En el repo: *Settings → Secrets and variables → Actions → New repository secret*,
   nombre `NTFY_TOPIC`, valor tu tema de ntfy. (El tema no queda en el código público.)
3. *Actions → Alerta granizo → Run workflow → modo `prueba`*: te tiene que llegar la notificación.
4. Listo: corre solo cada 5 min. GitHub a veces lo atrasa unos minutos en horas de mucha carga.

**En un VPS (lo más puntual)** — `crontab -e`:

```
*/5 * * * * NTFY_TOPIC=granizo-arroyoseco-x7k2 /usr/bin/python3 /home/ubuntu/granizo/alerta_granizo.py >> /home/ubuntu/granizo/log.txt 2>&1
```

**En Windows** (solo avisa con la PC prendida):

```
setx NTFY_TOPIC granizo-arroyoseco-x7k2
schtasks /create /tn "Alerta granizo" /sc minute /mo 5 /tr "python C:\ruta\alerta_granizo.py"
```

## Límites

- **Radar:** RainViewer (lo que usa Piedra Libre) hoy no tiene cobertura en el sur de Santa Fe;
  los radares del SMN no tienen API pública. Por eso la parte en vivo usa rayos.
- Los rayos dicen dónde hay tormenta fuerte, no si está cayendo piedra. Combinados con un
  pronóstico de SHIP alto son la mejor señal disponible gratis.
- Open-Meteo es gratis para uso no comercial (10.000 llamadas/día; esto usa 24).
- Con tormenta en la zona, cada corrida baja ~8 MB de NOAA (30 archivos GLM).
- Siempre mirar también las alertas oficiales: https://www.smn.gob.ar/alertas
