"""
Historico de Avisos Enel - Las Condes (v4)
CMU - Municipalidad de Las Condes

Descarga el feed publico de Enel (mapaemergencia.enel.com).

FILTRO:  limite comunal OFICIAL de Las Condes (Limite_Comunal_LasCondes.geojson,
         EPSG:4326), point-in-polygon INCLUSIVO: pasan los puntos que caen
         dentro del poligono o que tocan su borde (Polygon.covers).
VISUAL:  cada punto que pasa el filtro se etiqueta ademas con su
         h3_index (malla H3_LasCondes_Res8.geojson, resolucion 8) para
         mapearlo/agregarlo en Power BI por hexagono.

Cada ejecucion AGREGA la version actualizada al repositorio historico
(PostgreSQL); nunca sobreescribe lo anterior. Pensado para correr cada
10-15 min via Task Scheduler.

Requiere:
    pip install h3 shapely psycopg2-binary

Conexion a PostgreSQL (variables de entorno, con default entre parentesis):
    ENEL_DB_HOST     (localhost)
    ENEL_DB_PORT     (5432)
    ENEL_DB_NAME     (enel_las_condes)
    ENEL_DB_USER     (postgres)
    ENEL_DB_PASSWORD (vacio)

Archivos esperados en la misma carpeta que este script:
    Limite_Comunal_LasCondes.geojson  (limite comunal oficial, EPSG:4326,
                                        con campo "nom_com" = "LAS CONDES")
    H3_LasCondes_Res8.geojson          (malla H3 res. 8, solo
                                         para visualizacion)

Salidas (en la misma carpeta):
    enel_las_condes_eventos_activos.csv   -> eventos activos, para Power BI
    enel_las_condes_eventos_historico.csv -> historico completo, para Power BI
    enel_las_condes_log.txt               -> log de ejecucion
"""

import csv
import json
import logging
import os
import shutil
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import h3
import psycopg2
from shapely.geometry import Point, shape

# ----------------------------------------------------------------------
# CONFIGURACION
# ----------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
LIMITE_COMUNAL_PATH = BASE_DIR / "Limite_Comunal_LasCondes.geojson"
H3_GEOJSON_PATH = BASE_DIR / "H3_LasCondes_Res8.geojson"
CSV_ACTIVOS_PATH = BASE_DIR / "enel_las_condes_eventos_activos.csv"
CSV_HISTORICO_PATH = BASE_DIR / "enel_las_condes_eventos_historico.csv"
LOG_PATH = BASE_DIR / "enel_las_condes_log.txt"

ONEDRIVE_DIR = Path(
    r"C:\OneDrive\OneDrive - Municipalidad de Las Condes\Centro de Monitoreo Urbano"
    r" - 11-Informes tableros y reportes\07-Reporte_Enel"
)

DB_HOST = os.environ.get("ENEL_DB_HOST", "localhost")
DB_PORT = os.environ.get("ENEL_DB_PORT", "5432")
DB_NAME = os.environ.get("ENEL_DB_NAME", "enel_las_condes")
DB_USER = os.environ.get("ENEL_DB_USER", "postgres")
DB_PASSWORD = os.environ.get("ENEL_DB_PASSWORD", "")

URL_TEMPLATE = "https://mapaemergencia.enel.com/galeria/documento/me-capa-avisos.txt?&={ts}"
H3_RESOLUCION = 8  # debe coincidir con la resolucion de H3_LasCondes_Res8.geojson

NOMBRE_COMUNA_FILTRO = "Las Condes"

# ----------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8",
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger().addHandler(console)


# ----------------------------------------------------------------------
# LIMITE COMUNAL OFICIAL (FILTRO)
# ----------------------------------------------------------------------

def cargar_poligono_comunal():
    """Lee Limite_Comunal_LasCondes.geojson (EPSG:4326) y devuelve el
    poligono/multipoligono de Las Condes (feature cuyo properties.nom_com
    coincide con NOMBRE_COMUNA_FILTRO)."""
    with open(LIMITE_COMUNAL_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    feature_objetivo = None
    for feat in data.get("features", []):
        nombre = str(feat.get("properties", {}).get("nom_com", ""))
        if nombre.strip().lower() == NOMBRE_COMUNA_FILTRO.lower():
            feature_objetivo = feat
            break

    if feature_objetivo is None:
        raise RuntimeError(
            f"No se encontro un feature para la comuna '{NOMBRE_COMUNA_FILTRO}' "
            f"en {LIMITE_COMUNAL_PATH}"
        )

    poligono = shape(feature_objetivo["geometry"])

    logging.info(
        "Poligono comunal cargado desde %s: %s (valido=%s, area=%.6f)",
        LIMITE_COMUNAL_PATH.name, NOMBRE_COMUNA_FILTRO, poligono.is_valid, poligono.area,
    )
    return poligono


# ----------------------------------------------------------------------
# MALLA H3 (SOLO VISUALIZACION)
# ----------------------------------------------------------------------

def cargar_malla_h3() -> set:
    with open(H3_GEOJSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    indices = {feat["properties"]["h3_index"] for feat in data["features"]}
    logging.info("Malla H3 cargada: %d hexagonos (res %s)", len(indices), H3_RESOLUCION)
    return indices


def h3_de_punto(lat: float, lon: float) -> str:
    """h3_index del punto a la resolucion configurada (para mapear en Power BI,
    independiente de si el hexagono esta o no en la malla de referencia)."""
    try:
        return h3.latlng_to_cell(lat, lon, H3_RESOLUCION)
    except Exception:
        return None


# ----------------------------------------------------------------------
# DESCARGA
# ----------------------------------------------------------------------

def descargar_feed() -> dict:
    url = URL_TEMPLATE.format(ts=int(time.time() * 1000))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


# ----------------------------------------------------------------------
# BASE DE DATOS (REPOSITORIO HISTORICO EN POSTGRESQL)
# ----------------------------------------------------------------------

def conectar_db():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
    )


def init_db(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS eventos (
                cod_evento TEXT PRIMARY KEY,
                codigo TEXT,
                tipo TEXT,
                direccion TEXT,
                falla TEXT,
                desc_evento TEXT,
                id_alim TEXT,
                h3_index TEXT,
                en_malla_h3_referencia INTEGER,
                lon DOUBLE PRECISION,
                lat DOUBLE PRECISION,
                fecha_ini TEXT,
                fecha_reposicion_estimada TEXT,
                primera_vez_visto TEXT,
                ultima_vez_visto TEXT,
                clientes_afectados INTEGER,
                avisos_unicos TEXT,
                cod_avisos TEXT,
                ids_aviso TEXT,
                activo INTEGER DEFAULT 1,
                fecha_resolucion_detectada TEXT
            );

            CREATE TABLE IF NOT EXISTS historico_versiones (
                snapshot_ts TEXT,
                cod_evento TEXT,
                direccion TEXT,
                falla TEXT,
                id_alim TEXT,
                h3_index TEXT,
                lon DOUBLE PRECISION,
                lat DOUBLE PRECISION,
                fecha_ini TEXT,
                fecha_reposicion_estimada TEXT,
                clientes_afectados INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_hist_cod_evento ON historico_versiones(cod_evento);
            CREATE INDEX IF NOT EXISTS idx_hist_ts ON historico_versiones(snapshot_ts);
            """
        )
        # Defensivo: si la tabla ya existia de una version anterior del
        # script, agrega las columnas nuevas sin tocar los datos existentes.
        cur.execute("ALTER TABLE eventos ADD COLUMN IF NOT EXISTS tipo TEXT")
        cur.execute("ALTER TABLE eventos ADD COLUMN IF NOT EXISTS cod_avisos TEXT")
        cur.execute("ALTER TABLE eventos ADD COLUMN IF NOT EXISTS ids_aviso TEXT")
    conn.commit()


def upsert_evento(conn, snapshot_ts, cod_evento, props, h3_index, en_malla_ref,
                   lon, lat, clientes_afectados, avisos_unicos, cod_avisos, ids_aviso):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO eventos (
                cod_evento, codigo, tipo, direccion, falla, desc_evento, id_alim,
                h3_index, en_malla_h3_referencia, lon, lat, fecha_ini,
                fecha_reposicion_estimada, primera_vez_visto, ultima_vez_visto,
                clientes_afectados, avisos_unicos, cod_avisos, ids_aviso, activo
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
            ON CONFLICT (cod_evento) DO UPDATE SET
                falla = EXCLUDED.falla,
                fecha_reposicion_estimada = EXCLUDED.fecha_reposicion_estimada,
                ultima_vez_visto = EXCLUDED.ultima_vez_visto,
                clientes_afectados = EXCLUDED.clientes_afectados,
                avisos_unicos = EXCLUDED.avisos_unicos,
                cod_avisos = EXCLUDED.cod_avisos,
                ids_aviso = EXCLUDED.ids_aviso,
                activo = 1,
                fecha_resolucion_detectada = NULL
            """,
            (
                cod_evento, props.get("CODIGO"), props.get("TIPO"), props.get("DIRECCION"),
                props.get("FALLA"), props.get("DESC_EVENTO"), props.get("id_alim"),
                h3_index, int(en_malla_ref), lon, lat, props.get("FECHA_INI"),
                props.get("FECHA_REPOSICION"), snapshot_ts, snapshot_ts,
                clientes_afectados, avisos_unicos, cod_avisos, ids_aviso,
            ),
        )
        cur.execute(
            """
            INSERT INTO historico_versiones (
                snapshot_ts, cod_evento, direccion, falla, id_alim, h3_index,
                lon, lat, fecha_ini, fecha_reposicion_estimada, clientes_afectados
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                snapshot_ts, cod_evento, props.get("DIRECCION"), props.get("FALLA"),
                props.get("id_alim"), h3_index, lon, lat, props.get("FECHA_INI"),
                props.get("FECHA_REPOSICION"), clientes_afectados,
            ),
        )


def marcar_resueltos(conn, cod_eventos_vistos_hoy, snapshot_ts):
    with conn.cursor() as cur:
        cur.execute("SELECT cod_evento FROM eventos WHERE activo = 1")
        activos_previos = {r[0] for r in cur.fetchall()}
        recien_resueltos = activos_previos - cod_eventos_vistos_hoy

        if recien_resueltos:
            cur.execute(
                """
                UPDATE eventos SET activo = 0, fecha_resolucion_detectada = %s
                WHERE cod_evento = ANY(%s)
                """,
                (snapshot_ts, list(recien_resueltos)),
            )
    return len(recien_resueltos)


# ----------------------------------------------------------------------
# EXPORTS PARA POWER BI
# ----------------------------------------------------------------------

def _parsear_fecha_ini(valor):
    """FECHA_INI viene de Enel como 'DD-MM-YYYY HH:MM'."""
    if not valor:
        return None
    try:
        return datetime.strptime(valor, "%d-%m-%Y %H:%M")
    except ValueError:
        return None


def _parsear_snapshot(valor):
    """primera_vez_visto/fecha_resolucion_detectada usan 'YYYY-MM-DD HH:MM:SS'
    (formato propio, generado con datetime.now().strftime en main())."""
    if not valor:
        return None
    try:
        return datetime.strptime(valor, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _horas_activo(fecha_ini_str, fin_dt):
    """Horas transcurridas entre FechaInicio y `fin_dt` (la hora del reporte
    para eventos activos, o la hora de resolucion detectada para los ya
    resueltos). None si FechaInicio no se pudo interpretar."""
    fecha_ini = _parsear_fecha_ini(fecha_ini_str)
    if fecha_ini is None or fin_dt is None:
        return None
    return round((fin_dt - fecha_ini).total_seconds() / 3600, 1)


def exportar_csv(conn):
    ahora = datetime.now()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                cod_evento AS "CodigoEvento",
                codigo AS "Codigo",
                tipo AS "Tipo",
                direccion AS "Direccion",
                clientes_afectados AS "ClientesAfectados",
                fecha_ini AS "FechaInicio",
                fecha_reposicion_estimada AS "FechaReposicionEstimada",
                falla AS "DetalleFalla",
                desc_evento AS "DescripcionEvento",
                id_alim AS "Alimentador",
                h3_index AS "H3Index",
                en_malla_h3_referencia AS "EnMallaH3Referencia",
                lat AS "Latitud",
                lon AS "Longitud",
                avisos_unicos AS "ClientesUnicos",
                cod_avisos AS "CodigosAviso",
                ids_aviso AS "IdsAviso",
                primera_vez_visto AS "PrimeraVezVisto",
                ultima_vez_visto AS "UltimaVezVisto"
            FROM eventos
            WHERE activo = 1
            ORDER BY fecha_ini DESC
            """
        )
        cols = [d[0] for d in cur.description]
        idx_fecha_ini = cols.index("FechaInicio")
        filas = []
        for fila in cur.fetchall():
            fila = list(fila)
            fila.append(_horas_activo(fila[idx_fecha_ini], ahora))
            filas.append(fila)
    _escribir_csv(CSV_ACTIVOS_PATH, cols + ["HorasActivo"], filas)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                cod_evento AS "CodigoEvento",
                codigo AS "Codigo",
                tipo AS "Tipo",
                direccion AS "Direccion",
                clientes_afectados AS "ClientesAfectados",
                fecha_ini AS "FechaInicio",
                fecha_reposicion_estimada AS "FechaReposicionEstimada",
                falla AS "DetalleFalla",
                desc_evento AS "DescripcionEvento",
                id_alim AS "Alimentador",
                h3_index AS "H3Index",
                en_malla_h3_referencia AS "EnMallaH3Referencia",
                lat AS "Latitud",
                lon AS "Longitud",
                avisos_unicos AS "ClientesUnicos",
                cod_avisos AS "CodigosAviso",
                ids_aviso AS "IdsAviso",
                primera_vez_visto AS "PrimeraVezVisto",
                ultima_vez_visto AS "UltimaVezVisto",
                activo AS "Activo",
                fecha_resolucion_detectada AS "FechaResolucionDetectada"
            FROM eventos
            ORDER BY primera_vez_visto DESC
            """
        )
        cols = [d[0] for d in cur.description]
        idx_fecha_ini = cols.index("FechaInicio")
        idx_activo = cols.index("Activo")
        idx_fecha_resolucion = cols.index("FechaResolucionDetectada")
        filas = []
        for fila in cur.fetchall():
            fila = list(fila)
            if fila[idx_activo]:
                fin = ahora
            else:
                fin = _parsear_snapshot(fila[idx_fecha_resolucion]) or ahora
            fila.append(_horas_activo(fila[idx_fecha_ini], fin))
            filas.append(fila)
    _escribir_csv(CSV_HISTORICO_PATH, cols + ["HorasActivo"], filas)


def _escribir_csv(path, cols, filas):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(cols)
        writer.writerows(filas)


def copiar_csv_a_onedrive():
    """Copia los CSV ya generados a la carpeta compartida de OneDrive.
    No debe interrumpir la corrida programada si OneDrive esta
    sincronizando y el destino queda momentaneamente bloqueado."""
    for origen in (CSV_ACTIVOS_PATH, CSV_HISTORICO_PATH):
        destino = ONEDRIVE_DIR / origen.name
        try:
            shutil.copyfile(origen, destino)
            logging.info("Copiado a OneDrive: %s", destino)
        except OSError as e:
            logging.error("No se pudo copiar %s a OneDrive: %s", origen, e)


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main():
    snapshot_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info("=== Ejecucion iniciada (%s) ===", snapshot_ts)

    poligono_comunal = cargar_poligono_comunal()
    malla_h3_referencia = cargar_malla_h3()

    try:
        data = descargar_feed()
    except Exception as e:
        logging.error("Error al descargar el feed: %s", e)
        sys.exit(1)

    features = data.get("features", [])
    logging.info("Total registros en feed (todo Enel): %d", len(features))

    if not features:
        logging.warning(
            "El feed llego vacio (0 registros en todo Enel); probable falla "
            "transitoria de la API. No se actualiza el repositorio en esta "
            "corrida (no se marca ningun evento como resuelto)."
        )
        return

    # 1) Filtro real: limite comunal oficial (point-in-polygon inclusivo:
    #    pasan los puntos dentro del poligono o que tocan su borde)
    seleccionados = []
    for feat in features:
        coords = feat.get("geometry", {}).get("coordinates", [None, None])
        lon, lat = (coords + [None, None])[:2]
        if lon is None or lat is None:
            continue
        if poligono_comunal.covers(Point(lon, lat)):
            h3_index = h3_de_punto(lat, lon)
            en_malla_ref = h3_index in malla_h3_referencia if h3_index else False
            seleccionados.append((feat, h3_index, en_malla_ref, lon, lat))

    logging.info("Registros dentro del limite oficial de Las Condes: %d", len(seleccionados))

    # 1b) Descartar registros sin identificador de evento (no se puede llevar
    # historico confiable y colisionarian en la clave primaria de la tabla)
    sin_cod_evento = 0
    seleccionados_validos = []
    for entry in seleccionados:
        feat = entry[0]
        props = feat.get("properties", {})
        if not (props.get("COD_EVENTO") or props.get("CODIGO")):
            sin_cod_evento += 1
            continue
        seleccionados_validos.append(entry)
    if sin_cod_evento:
        logging.warning(
            "Se descartaron %d registros dentro de Las Condes sin COD_EVENTO/CODIGO",
            sin_cod_evento,
        )
    seleccionados = seleccionados_validos

    # 2) Agrupar por evento (COD_EVENTO) las columnas que vienen una fila por
    # cliente/aviso individual, para tener el detalle completo del evento
    clientes_por_evento = {}
    cod_avisos_por_evento = {}
    ids_aviso_por_evento = {}
    for feat, _, _, _, _ in seleccionados:
        props = feat.get("properties", {})
        cod_evento = props.get("COD_EVENTO") or props.get("CODIGO")
        clientes_por_evento.setdefault(cod_evento, set()).add(props.get("numero_cliente"))
        cod_avisos_por_evento.setdefault(cod_evento, set()).add(props.get("COD_AVISO"))
        ids_aviso_por_evento.setdefault(cod_evento, set()).add(props.get("ID_AVISO"))

    conn = conectar_db()
    try:
        init_db(conn)

        cod_eventos_hoy = set()
        for feat, h3_index, en_malla_ref, lon, lat in seleccionados:
            props = feat.get("properties", {})
            cod_evento = props.get("COD_EVENTO") or props.get("CODIGO")
            cod_eventos_hoy.add(cod_evento)

            clientes_set = clientes_por_evento.get(cod_evento, set())
            clientes_afectados = len(clientes_set)
            avisos_unicos = ",".join(str(c) for c in sorted(clientes_set, key=str))
            cod_avisos = ",".join(str(c) for c in sorted(cod_avisos_por_evento.get(cod_evento, set()), key=str))
            ids_aviso = ",".join(str(c) for c in sorted(ids_aviso_por_evento.get(cod_evento, set()), key=str))

            upsert_evento(
                conn, snapshot_ts, cod_evento, props, h3_index, en_malla_ref,
                lon, lat, clientes_afectados, avisos_unicos, cod_avisos, ids_aviso,
            )

        n_resueltos = marcar_resueltos(conn, cod_eventos_hoy, snapshot_ts)
        conn.commit()

        exportar_csv(conn)
    finally:
        conn.close()

    copiar_csv_a_onedrive()

    logging.info("Eventos activos en esta corrida: %d", len(cod_eventos_hoy))
    logging.info("Eventos marcados como resueltos en esta corrida: %d", n_resueltos)
    logging.info("CSV activos: %s", CSV_ACTIVOS_PATH)
    logging.info("CSV historico: %s", CSV_HISTORICO_PATH)
    logging.info("=== Ejecucion finalizada ===\n")


if __name__ == "__main__":
    main()
