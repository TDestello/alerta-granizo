#!/usr/bin/env python3
"""
Alerta de granizo para Arroyo Seco (Santa Fe): pronóstico + rayos en vivo.

Pipeline (correrlo cada 5 minutos):
  1. PRONÓSTICO (se renueva cada hora): descarga GFS y ECMWF desde Open-Meteo y calcula
     hora por hora el SHIP (Significant Hail Parameter, índice del SPC/NOAA) a partir de
     CAPE, humedad, gradiente térmico 700-500 hPa, temperatura en 500 hPa, cortante del
     viento 0-6 km y altura de la isoterma de 0 °C. Avisa con horas/un día de anticipación.
  2. EN VIVO: si el ambiente está para tormentas, descarga los rayos de los últimos minutos
     del satélite GOES-19 (sensor GLM, NOAA) y mira a qué distancia caen, si se acercan y si
     hay un salto brusco en la cantidad de rayos (señal típica de una celda que da granizo).
     Avisa con minutos de anticipación.
  3. Manda las notificaciones (ntfy.sh y/o Telegram) y guarda lo avisado para no repetir.

Requisitos: Python 3.9+. Para los rayos además: pip install h5py
(sin h5py el pronóstico funciona igual y los rayos se saltean).

Uso:
  python alerta_granizo.py              # analiza y avisa si corresponde
  python alerta_granizo.py --dry-run    # muestra todo, no manda nada
  python alerta_granizo.py --forzar     # mira los rayos aunque el pronóstico esté tranquilo
  python alerta_granizo.py --test       # manda una notificación de prueba

Configuración por variables de entorno (todas opcionales):
  LAT, LON, LUGAR          ubicación (por defecto Arroyo Seco)
  MODELOS                  modelos de Open-Meteo separados por coma (gfs_seamless,ecmwf_ifs025)
  HORAS                    horizonte del pronóstico en horas (48)
  VENTANA_RAYOS            minutos de rayos a mirar en cada corrida (10)
  NTFY_TOPIC               tema de ntfy.sh, p. ej. "granizo-arroyoseco-x7k2"
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  ESTADO                   archivo donde se guardan los avisos enviados y el pronóstico
"""

import argparse
import io
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

LAT = float(os.getenv("LAT", "-33.1547"))
LON = float(os.getenv("LON", "-60.5086"))
LUGAR = os.getenv("LUGAR", "Arroyo Seco, Santa Fe")
MODELOS = os.getenv("MODELOS", "gfs_seamless,ecmwf_ifs025").split(",")
HORAS = int(os.getenv("HORAS", "48"))
VENTANA_RAYOS = int(os.getenv("VENTANA_RAYOS", "10"))
ESTADO = Path(os.getenv("ESTADO", Path(__file__).with_name("estado_alertas.json")))
TZ = "America/Argentina/Buenos_Aires"
ART = timezone(timedelta(hours=-3))  # Argentina no tiene horario de verano

VARIABLES = [
    "cape", "lifted_index", "freezing_level_height",
    "temperature_2m", "dew_point_2m", "surface_pressure",
    "temperature_500hPa", "temperature_700hPa",
    "geopotential_height_500hPa", "geopotential_height_700hPa",
    "wind_speed_10m", "wind_direction_10m",
    "wind_speed_500hPa", "wind_direction_500hPa",
    "precipitation_probability", "precipitation", "weather_code",
]

NIVELES = ["NINGUNO", "BAJO", "MODERADO", "ALTO"]
PRIORIDAD = {1: "default", 2: "high", 3: "urgent"}
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
RUMBOS = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]
NOMBRE_MODELO = {"gfs_seamless": "GFS", "ecmwf_ifs025": "ECMWF"}


def ahora():
    """Hora local de Argentina sin zona, para comparar con las horas de Open-Meteo."""
    return datetime.now(ART).replace(tzinfo=None)


# ---------------------------------------------------------------- pronóstico: datos

def descargar_pronostico(estado):
    cache = estado.get("cache_pronostico")
    if cache and ahora() - datetime.fromisoformat(cache["descargado"]) < timedelta(minutes=55):
        return cache["hourly"]
    params = {
        "latitude": LAT,
        "longitude": LON,
        "hourly": ",".join(VARIABLES),
        "models": ",".join(MODELOS),
        "forecast_days": 3,
        "timezone": TZ,
        "wind_speed_unit": "ms",
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        datos = json.load(r)
    if datos.get("error"):
        raise RuntimeError(datos.get("reason"))
    estado["cache_pronostico"] = {"descargado": ahora().isoformat(), "hourly": datos["hourly"]}
    return datos["hourly"]


def serie(hourly, var, modelo):
    # Con varios modelos, Open-Meteo agrega el sufijo _<modelo> a cada variable.
    return hourly.get(f"{var}_{modelo}") or hourly.get(var) or []


# ---------------------------------------------------------------- pronóstico: física

def razon_mezcla(td_c, p_hpa):
    """Razón de mezcla (g/kg) a partir del punto de rocío y la presión."""
    e = 6.112 * math.exp(17.67 * td_c / (td_c + 243.5))
    return 621.97 * e / (p_hpa - e)


def componentes(vel, dir_deg):
    rad = math.radians(dir_deg)
    return -vel * math.sin(rad), -vel * math.cos(rad)


def cortante_0_6km(v10, d10, v500, d500):
    u1, w1 = componentes(v10, d10)
    u2, w2 = componentes(v500, d500)
    return math.hypot(u2 - u1, w2 - w1)


def isoterma_cero(t700, z700, t500, z500, fl_modelo):
    if fl_modelo is not None:
        return fl_modelo
    # Interpolación lineal entre 700 y 500 hPa (ECMWF no la publica en Open-Meteo).
    if t700 <= 0:
        # Ya bajo cero en 700 hPa: estimar hacia abajo con 6.5 °C/km.
        return z700 + t700 / 6.5 * 1000
    return z700 + (z500 - z700) * t700 / (t700 - t500)


def ship(cape, mr, lr, t500, cort, fl):
    """Significant Hail Parameter (SPC). >0.5 granizo posible, >1 granizo grande probable."""
    if cape is None or cape <= 0:
        return 0.0
    mr = min(max(mr, 11.0), 13.6)
    cort = min(max(cort, 7.0), 27.0)
    t500 = min(t500, -5.5)
    s = cape * mr * lr * (-t500) * cort / 42_000_000
    if cape < 1300:
        s *= cape / 1300
    if lr < 5.8:
        s *= lr / 5.8
    if fl < 2400:
        s *= fl / 2400
    return max(s, 0.0)


def nivel_riesgo(s, cape, prob):
    # Sin probabilidad de tormenta no hay granizo aunque la atmósfera sea favorable.
    if prob is not None and prob < 20:
        return 0
    if s >= 1.0:
        n = 3
    elif s >= 0.5:
        n = 2
    elif s >= 0.2 and cape >= 500:
        n = 1
    else:
        n = 0
    if n and prob is not None and prob < 40:
        n -= 1
    return n


# ---------------------------------------------------------------- pronóstico: análisis

def analizar(hourly, desde_h=0, hasta_h=HORAS):
    t0 = ahora().replace(minute=0, second=0, microsecond=0)
    inicio, limite = t0 + timedelta(hours=desde_h), t0 + timedelta(hours=hasta_h)
    filas = []
    for i, t in enumerate(hourly["time"]):
        hora = datetime.fromisoformat(t)
        if hora < inicio or hora > limite:
            continue
        por_modelo = {}
        for m in MODELOS:
            g = lambda v: (serie(hourly, v, m) or [None] * (i + 1))[i]
            req = [g(v) for v in ("cape", "dew_point_2m", "surface_pressure", "temperature_500hPa",
                                  "temperature_700hPa", "geopotential_height_500hPa",
                                  "geopotential_height_700hPa", "wind_speed_10m", "wind_direction_10m",
                                  "wind_speed_500hPa", "wind_direction_500hPa")]
            if any(x is None for x in req):
                continue
            cape, td, ps, t500, t700, z500, z700, v10, d10, v500, d500 = req
            lr = (t700 - t500) / ((z500 - z700) / 1000)
            fl = isoterma_cero(t700, z700, t500, z500, g("freezing_level_height"))
            cort = cortante_0_6km(v10, d10, v500, d500)
            s = ship(cape, razon_mezcla(td, ps), lr, t500, cort, fl)
            prob = g("precipitation_probability")
            por_modelo[m] = {
                "ship": round(s, 2), "cape": cape, "prob": prob, "cortante": round(cort, 1),
                "isoterma0": round(fl), "codigo": g("weather_code"),
                "nivel": nivel_riesgo(s, cape, prob),
            }
        if por_modelo:
            filas.append({"hora": hora, "modelos": por_modelo,
                          "nivel": max(d["nivel"] for d in por_modelo.values())})
    return filas


def resumir(filas):
    """Agrupa las horas con riesgo en eventos (horas consecutivas) y devuelve el peor por evento."""
    eventos, actual = [], None
    for f in filas:
        if f["nivel"] == 0:
            actual = None
            continue
        if actual and f["hora"] - actual["fin"] <= timedelta(hours=2):
            actual["fin"] = f["hora"]
            if f["nivel"] > actual["nivel"] or (f["nivel"] == actual["nivel"] and max_ship(f) > max_ship(actual["pico"])):
                actual["nivel"], actual["pico"] = f["nivel"], f
        else:
            actual = {"inicio": f["hora"], "fin": f["hora"], "nivel": f["nivel"], "pico": f}
            eventos.append(actual)
    return eventos


def max_ship(fila):
    return max(d["ship"] for d in fila["modelos"].values())


def mensaje_evento(ev):
    p = ev["pico"]
    coinciden = [NOMBRE_MODELO.get(m, m) for m, d in p["modelos"].items() if d["nivel"] > 0]
    detalle = "\n".join(
        f"  {NOMBRE_MODELO.get(m, m)}: SHIP {d['ship']}, CAPE {d['cape']:.0f} J/kg, "
        f"lluvia {d['prob']}%, cortante {d['cortante']} m/s, 0 °C a {d['isoterma0']} m"
        for m, d in p["modelos"].items()
    )
    return (
        f"Riesgo {NIVELES[ev['nivel']]} de granizo en {LUGAR}\n"
        f"Ventana: {DIAS[ev['inicio'].weekday()]} {ev['inicio']:%d/%m %H:%M} a "
        f"{ev['fin'] + timedelta(hours=1):%H:%M} (pico {p['hora']:%H:%M})\n"
        f"Lo marcan: {', '.join(coinciden)}\n{detalle}"
    )


def hay_ambiente_de_tormenta(hourly):
    """¿Vale la pena mirar los rayos? CAPE o probabilidad de lluvia algo altas cerca de ahora."""
    for f in analizar(hourly, desde_h=-2, hasta_h=3):
        for d in f["modelos"].values():
            if d["cape"] >= 200 or (d["prob"] or 0) >= 30:
                return True
    return False


# ---------------------------------------------------------------- en vivo: rayos GOES-19 GLM

GLM_BUCKET = "https://noaa-goes19.s3.amazonaws.com"
RADIO_RAYOS = 100  # km que se tienen en cuenta


def listar_glm(desde):
    """Archivos GLM (uno cada 20 s) que empezaron después de `desde` (UTC)."""
    claves = []
    for hora in sorted({desde.strftime("%Y/%j/%H"), datetime.now(timezone.utc).strftime("%Y/%j/%H")}):
        url = f"{GLM_BUCKET}/?list-type=2&prefix=GLM-L2-LCFA/{hora}/"
        with urllib.request.urlopen(url, timeout=30) as r:
            claves += re.findall(r"<Key>(GLM-L2-LCFA/[^<]+\.nc)</Key>", r.read().decode())
    archivos = []
    for k in claves:
        inicio = datetime.strptime(re.search(r"_s(\d{13})", k).group(1), "%Y%j%H%M%S")
        inicio = inicio.replace(tzinfo=timezone.utc)
        if inicio >= desde:
            archivos.append((inicio, k))
    return archivos


def leer_glm(clave):
    import h5py
    with urllib.request.urlopen(f"{GLM_BUCKET}/{clave}", timeout=60) as r:
        buf = io.BytesIO(r.read())
    with h5py.File(buf, "r") as f:
        return f["flash_lat"][:].tolist(), f["flash_lon"][:].tolist()


def distancia_rumbo(lat, lon):
    """Distancia (km) y rumbo (°) desde el lugar hasta el punto."""
    p1, p2 = math.radians(LAT), math.radians(lat)
    dl = math.radians(lon - LON)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d = 2 * 6371 * math.asin(math.sqrt(a))
    rumbo = math.degrees(math.atan2(math.sin(dl) * math.cos(p2),
                                    math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)))
    return d, rumbo % 360


def descargar_rayos(minutos):
    """Lista de (hora UTC, distancia km, rumbo °) de los rayos cercanos de los últimos minutos."""
    archivos = listar_glm(datetime.now(timezone.utc) - timedelta(minutes=minutos))
    with ThreadPoolExecutor(8) as ex:
        contenidos = list(ex.map(lambda a: leer_glm(a[1]), archivos))
    rayos = []
    for (inicio, _), (lats, lons) in zip(archivos, contenidos):
        for la, lo in zip(lats, lons):
            if abs(la - LAT) > 1.5 or abs(lo - LON) > 1.8:
                continue  # descarte rápido de lo que está lejos
            d, rumbo = distancia_rumbo(la, lo)
            if d <= RADIO_RAYOS:
                rayos.append((inicio, d, rumbo))
    return rayos, len(archivos)


def analizar_rayos(rayos, minutos, riesgo_pronostico):
    """Clasifica la situación en vivo. Devuelve (nivel 0-3, texto)."""
    if not rayos:
        return 0, "sin rayos a menos de %d km" % RADIO_RAYOS
    mitad = datetime.now(timezone.utc) - timedelta(minutes=minutos / 2)
    recientes = [r for r in rayos if r[0] >= mitad]
    anteriores = [r for r in rayos if r[0] < mitad]
    mas_cerca = min(rayos, key=lambda r: r[1])
    dist = mas_cerca[1]
    rumbo = RUMBOS[round(mas_cerca[2] / 45) % 8]

    nivel = 3 if dist <= 15 else 2 if dist <= 40 else 1
    lineas = [f"Rayo más cercano a {dist:.0f} km al {rumbo} ({len(rayos)} rayos a <{RADIO_RAYOS} km "
              f"en los últimos {minutos} min)"]

    # ¿Se acerca? Comparo la distancia media de la primera y la segunda mitad de la ventana.
    if len(recientes) >= 3 and len(anteriores) >= 3:
        d_ant = sum(r[1] for r in anteriores) / len(anteriores)
        d_rec = sum(r[1] for r in recientes) / len(recientes)
        velocidad = (d_ant - d_rec) / (minutos / 2)  # km/min
        if 0.3 < velocidad <= 2:
            lineas.append(f"Se acerca a ~{velocidad * 60:.0f} km/h: llegaría en ~{dist / velocidad:.0f} min")
        elif velocidad > 2:
            # Más de 120 km/h no es un desplazamiento real: son celdas nuevas formándose más cerca.
            lineas.append("Se están formando celdas nuevas más cerca")
        elif velocidad < -0.3:
            lineas.append("Se está alejando")

    # Salto brusco de rayos: precursor conocido de granizo y viento fuerte.
    salto = len(recientes) >= 10 and len(recientes) >= 2 * max(len(anteriores), 1)
    if salto:
        lineas.append(f"Salto brusco de rayos ({len(anteriores)} → {len(recientes)}): señal típica de granizo")
        nivel = max(nivel, 2)

    if riesgo_pronostico >= 2:
        lineas.append(f"El pronóstico marca ambiente de granizo ({NIVELES[riesgo_pronostico]})")
    elif nivel == 1 and not salto:
        nivel = 0  # tormenta lejana y sin ambiente de granizo: no molestar
    return nivel, "\n".join(lineas)


# ---------------------------------------------------------------- avisos y estado

def enviar(titulo, texto, prioridad="high"):
    enviado = False
    topic = os.getenv("NTFY_TOPIC")
    if topic:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=texto.encode(),
            headers={"Title": titulo, "Priority": prioridad, "Tags": "cloud_with_lightning_and_rain"})
        urllib.request.urlopen(req, timeout=15)
        enviado = True
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        data = urllib.parse.urlencode({"chat_id": chat, "text": f"{titulo}\n\n{texto}"}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=15)
        enviado = True
    if not enviado:
        print("(sin canal configurado: definí NTFY_TOPIC o TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)")
    return enviado


def cargar_estado():
    try:
        estado = json.loads(ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        estado = {}
    estado.setdefault("pronostico", {})
    estado.setdefault("vivo", {})
    return estado


def guardar_estado(estado):
    corte = (ahora() - timedelta(days=3)).isoformat()
    estado["pronostico"] = {k: v for k, v in estado["pronostico"].items() if v["avisado"] > corte}
    ESTADO.write_text(json.dumps(estado, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- main

def imprimir_tabla(filas):
    print(f"{'hora':<12}" + "".join(f"{NOMBRE_MODELO.get(m, m):>34}" for m in MODELOS) + "   riesgo")
    for f in filas:
        cols = ""
        for m in MODELOS:
            d = f["modelos"].get(m)
            cols += (f"{'SHIP ' + str(d['ship']):>12} CAPE {d['cape']:>5.0f} {str(d['prob']) + '%':>5}  "
                     if d else f"{'-':>34}")
        print(f"{f['hora']:%d/%m %H:%M}  {cols}  {NIVELES[f['nivel']]}")


def paso_pronostico(hourly, estado, dry_run):
    filas = analizar(hourly)
    if dry_run:
        imprimir_tabla(filas)
        print()
    eventos = resumir(filas)
    if not eventos:
        print(f"{ahora():%d/%m %H:%M} pronóstico: sin riesgo de granizo en las próximas {HORAS} h")
    for ev in eventos:
        texto = mensaje_evento(ev)
        print(texto, end="\n\n")
        clave = ev["pico"]["hora"].strftime("%Y-%m-%d")  # un evento por día
        previo = estado["pronostico"].get(clave, {}).get("nivel", 0)
        if ev["nivel"] <= previo or dry_run:
            continue  # ya avisado con este nivel o mayor
        if enviar(f"Granizo: riesgo {NIVELES[ev['nivel']]}", texto, PRIORIDAD[ev["nivel"]]):
            estado["pronostico"][clave] = {"nivel": ev["nivel"], "avisado": ahora().isoformat()}
    # Riesgo de las próximas horas, para darle contexto a los rayos.
    cercanas = analizar(hourly, desde_h=-2, hasta_h=6)
    return max((f["nivel"] for f in cercanas), default=0)


def paso_rayos(hourly, estado, riesgo, dry_run, forzar):
    if not forzar and not hay_ambiente_de_tormenta(hourly):
        print(f"{ahora():%d/%m %H:%M} rayos: no hay ambiente de tormenta, no se miran")
        return
    try:
        import h5py  # noqa: F401
    except ImportError:
        print("rayos: falta h5py (pip install h5py), se saltea")
        return
    rayos, n_archivos = descargar_rayos(VENTANA_RAYOS)
    nivel, texto = analizar_rayos(rayos, VENTANA_RAYOS, riesgo)
    print(f"{ahora():%d/%m %H:%M} rayos ({n_archivos} archivos GLM): {texto}")
    if nivel == 0 or dry_run:
        return
    previo = estado["vivo"]
    hace = ahora() - datetime.fromisoformat(previo.get("avisado", "2000-01-01T00:00"))
    # Avisar si empeora, o recordar cada 45 min mientras siga activo.
    if nivel > previo.get("nivel", 0) or hace > timedelta(minutes=45):
        titulo = {1: "Tormenta en la zona", 2: "Tormenta cerca", 3: "Tormenta encima"}[nivel]
        if enviar(f"{titulo} - {LUGAR}", texto, PRIORIDAD[nivel]):
            estado["vivo"] = {"nivel": nivel, "avisado": ahora().isoformat()}
    elif hace > timedelta(minutes=90):
        estado["vivo"] = {}


def main():
    ap = argparse.ArgumentParser(description="Alerta de granizo: pronóstico Open-Meteo + rayos GOES-19")
    ap.add_argument("--dry-run", action="store_true", help="no manda avisos, muestra todo")
    ap.add_argument("--test", action="store_true", help="manda una notificación de prueba")
    ap.add_argument("--forzar", action="store_true", help="mira los rayos aunque no haya ambiente de tormenta")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # la consola de Windows rompe los acentos

    if args.test:
        ok = enviar("Prueba alerta granizo", f"Si ves esto, los avisos para {LUGAR} funcionan.", "default")
        sys.exit(0 if ok else 1)

    estado = cargar_estado()
    hourly = descargar_pronostico(estado)
    riesgo = paso_pronostico(hourly, estado, args.dry_run)
    try:
        paso_rayos(hourly, estado, riesgo, args.dry_run, args.forzar)
    except Exception as e:  # que una caída de NOAA no frene el pronóstico
        print(f"rayos: error {e}")
    guardar_estado(estado)


if __name__ == "__main__":
    main()
