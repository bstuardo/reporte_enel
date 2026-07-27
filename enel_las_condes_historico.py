"""
Historico de Avisos Enel - Las Condes (v5)
CMU - Municipalidad de Las Condes

Descarga los 4 feeds publicos de Enel (mapaemergencia.enel.com):
    1) me-capa-avisos.txt            -> avisos individuales de clientes (Point)
    2) me-capa-trafosAfectados.txt   -> transformadores afectados, TIPO=TRAFO
                                         (mezclado con TIPO=DESCARGO en el mismo
                                         feed; solo se usa la parte TRAFO aqui)
    3) me-capa-descargos.txt         -> cortes programados/mantenimiento (Polygon)
    4) me-capa-estado.txt            -> health-check general (JSON simple)

FILTRO:  limite comunal OFICIAL de Las Condes (Limite_Comunal_LasCondes.geojson,
         EPSG:4326), point-in-polygon INCLUSIVO: pasan los puntos que caen
         dentro del poligono o que tocan su borde (Polygon.covers). Para los
         feeds 2 y 3 (geometria Polygon) se usa el representative_point() de
         cada poligono, que a diferencia del centroide queda garantizado
         dentro de la forma.
VISUAL:  cada registro que pasa el filtro se etiqueta ademas con su
         h3_index (malla H3_LasCondes_Res8.geojson, resolucion 8) para
         mapearlo/agregarlo en Power BI por hexagono.
CRUCE:   los feeds 2 y 3 comparten INCIDENCIA con COD_EVENTO/CODIGO del feed 1;
         se usa para enriquecerlos con direccion(es) y clientes afectados.

Cada ejecucion AGREGA la version actualizada al repositorio historico
(PostgreSQL); nunca sobreescribe lo anterior. Pensado para correr cada
10-30 min via Task Scheduler. Cada feed se descarga y procesa de forma
independiente: si uno falla (timeout, JSON corrupto, etc.) los demas
siguen su curso normal y el fallo queda en el log (no se marca nada de
ese feed como resuelto en la corrida donde fallo).

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
    enel_las_condes_eventos_activos.csv   -> avisos activos, para Power BI
    enel_las_condes_eventos_historico.csv -> avisos, historico completo
    enel_las_condes_trafos_activos.csv    -> transformadores afectados activos
    enel_las_condes_descargos.csv         -> descargos programados (todos)
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
from datetime import datetime, timedelta
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
CSV_TRAFOS_ACTIVOS_PATH = BASE_DIR / "enel_las_condes_trafos_activos.csv"
CSV_DESCARGOS_PATH = BASE_DIR / "enel_las_condes_descargos.csv"
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

# Replica opcional en Supabase (para Superset u otro visualizador externo).
# Si SUPABASE_DB_HOST queda vacio, se omite por completo sin afectar la
# corrida normal contra el Postgres local.
SUPABASE_DB_HOST = os.environ.get("SUPABASE_DB_HOST", "")
SUPABASE_DB_PORT = os.environ.get("SUPABASE_DB_PORT", "5432")
SUPABASE_DB_NAME = os.environ.get("SUPABASE_DB_NAME", "postgres")
SUPABASE_DB_USER = os.environ.get("SUPABASE_DB_USER", "postgres")
SUPABASE_DB_PASSWORD = os.environ.get("SUPABASE_DB_PASSWORD", "")

URL_TEMPLATE = "https://mapaemergencia.enel.com/galeria/documento/me-capa-avisos.txt?&={ts}"
URL_TRAFOS_TEMPLATE = "https://mapaemergencia.enel.com/galeria/documento/me-capa-trafosAfectados.txt?&={ts}"
URL_DESCARGOS_TEMPLATE = "https://mapaemergencia.enel.com/galeria/documento/me-capa-descargos.txt?&={ts}"
URL_ESTADO_TEMPLATE = "https://mapaemergencia.enel.com/galeria/documento/me-capa-estado.txt?&={ts}"
H3_RESOLUCION = 8  # debe coincidir con la resolucion de H3_LasCondes_Res8.geojson

NOMBRE_COMUNA_FILTRO = "Las Condes"

# RNF-07: retencion de las tablas de snapshots crudos (*_versiones). Las
# tablas de resumen (eventos, trafos_afectados, descargos_programados)
# NO se purgan nunca: conservan el estado/resumen actual de cada uno,
# independiente de su antiguedad.
RETENCION_DIAS_HISTORICO = int(os.environ.get("ENEL_RETENCION_DIAS_HISTORICO", "90"))

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

def _descargar_json(url_template: str) -> dict:
    url = url_template.format(ts=int(time.time() * 1000))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def descargar_feed() -> dict:
    return _descargar_json(URL_TEMPLATE)


def descargar_trafos() -> dict:
    return _descargar_json(URL_TRAFOS_TEMPLATE)


def descargar_descargos() -> dict:
    return _descargar_json(URL_DESCARGOS_TEMPLATE)


def descargar_estado() -> dict:
    return _descargar_json(URL_ESTADO_TEMPLATE)


# ----------------------------------------------------------------------
# FILTRO COMUNAL PARA GEOMETRIAS POLYGON (feeds 2 y 3)
# ----------------------------------------------------------------------

def _filtrar_por_comuna_poligonos(features, tipo_esperado, poligono_comunal, malla_h3_referencia):
    """Filtra features de geometria Polygon (trafosAfectados/descargos) al
    limite comunal, usando el representative_point() de cada poligono
    (a diferencia del centroide, queda garantizado dentro de la forma).

    `tipo_esperado`: si se indica, descarta features cuyo properties.TIPO
    no coincida (ej. "TRAFO" para separar los transformadores de los
    descargos programados que vienen mezclados en el mismo feed)."""
    seleccionados = []
    for feat in features:
        props = feat.get("properties", {})
        if tipo_esperado and props.get("TIPO") != tipo_esperado:
            continue
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            poligono = shape(geom)
            punto = poligono.representative_point()
        except Exception:
            continue
        if poligono_comunal.covers(punto):
            h3_index = h3_de_punto(punto.y, punto.x)
            en_malla_ref = h3_index in malla_h3_referencia if h3_index else False
            seleccionados.append((feat, h3_index, en_malla_ref, punto.x, punto.y))
    return seleccionados


def _consolidar_avisos_por_incidencia(seleccionados_avisos):
    """A partir de los avisos (feed 1) ya filtrados a Las Condes, arma un
    diccionario COD_EVENTO/CODIGO -> direcciones y clientes distintos, para
    cruzarlo con trafosAfectados/descargos via su campo INCIDENCIA (RF-05)."""
    consolidado = {}
    for feat, _, _, _, _ in seleccionados_avisos:
        props = feat.get("properties", {})
        cod_evento = props.get("COD_EVENTO") or props.get("CODIGO")
        if not cod_evento:
            continue
        entry = consolidado.setdefault(cod_evento, {"direcciones": set(), "clientes": set()})
        direccion = props.get("DIRECCION")
        if direccion and direccion.strip():
            entry["direcciones"].add(direccion.strip())
        entry["clientes"].add(props.get("numero_cliente"))
    return consolidado


def _clientes_afectados_poligono(props, clientes_fallback):
    """RF-06: preferir CLITOTAL (oficial de Enel) cuando venga informado;
    si no, usar el conteo de numero_cliente distintos cruzados desde avisos."""
    cli_total = (props.get("CLITOTAL") or "").strip()
    if cli_total.isdigit():
        return int(cli_total)
    return len(clientes_fallback)


def _preparar_filas_polygon(seleccionados, avisos_por_incidencia):
    """Igual proposito que _preparar_filas, para trafosAfectados/descargos:
    calcula una sola vez las direcciones/clientes cruzados via INCIDENCIA."""
    filas = []
    for feat, h3_index, en_malla_ref, lon, lat in seleccionados:
        props = feat.get("properties", {})
        numpos = props.get("numpos")
        if not numpos:
            continue
        incidencia = props.get("INCIDENCIA")
        info = avisos_por_incidencia.get(incidencia, {"direcciones": set(), "clientes": set()})
        direcciones = ",".join(sorted(info["direcciones"]))
        clientes_afectados = _clientes_afectados_poligono(props, info["clientes"])
        filas.append((numpos, props, h3_index, en_malla_ref, lon, lat, clientes_afectados, direcciones))
    return filas


# ----------------------------------------------------------------------
# BASE DE DATOS (REPOSITORIO HISTORICO EN POSTGRESQL)
# ----------------------------------------------------------------------

def conectar_db():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
    )


def conectar_supabase():
    return psycopg2.connect(
        host=SUPABASE_DB_HOST, port=SUPABASE_DB_PORT, dbname=SUPABASE_DB_NAME,
        user=SUPABASE_DB_USER, password=SUPABASE_DB_PASSWORD,
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

            -- Feed 2: transformadores afectados (TIPO=TRAFO en trafosAfectados.txt)
            CREATE TABLE IF NOT EXISTS trafos_afectados (
                numpos TEXT PRIMARY KEY,
                incidencia TEXT,
                tipo TEXT,
                tension TEXT,
                id_alim TEXT,
                h3_index TEXT,
                en_malla_h3_referencia INTEGER,
                lon DOUBLE PRECISION,
                lat DOUBLE PRECISION,
                fecha_inicio TEXT,
                estadoinc TEXT,
                fecha_reposicion TEXT,
                direcciones TEXT,
                clientes_afectados INTEGER,
                primera_vez_visto TEXT,
                ultima_vez_visto TEXT,
                activo INTEGER DEFAULT 1,
                fecha_resolucion_detectada TEXT
            );

            CREATE TABLE IF NOT EXISTS trafos_versiones (
                snapshot_ts TEXT,
                numpos TEXT,
                incidencia TEXT,
                estadoinc TEXT,
                fecha_reposicion TEXT,
                clientes_afectados INTEGER,
                direcciones TEXT,
                h3_index TEXT,
                lon DOUBLE PRECISION,
                lat DOUBLE PRECISION
            );

            CREATE INDEX IF NOT EXISTS idx_trafos_hist_numpos ON trafos_versiones(numpos);
            CREATE INDEX IF NOT EXISTS idx_trafos_hist_ts ON trafos_versiones(snapshot_ts);

            -- Feed 3: descargos programados (me-capa-descargos.txt)
            CREATE TABLE IF NOT EXISTS descargos_programados (
                numpos TEXT PRIMARY KEY,
                incidencia TEXT,
                tipo TEXT,
                tension TEXT,
                id_alim TEXT,
                h3_index TEXT,
                en_malla_h3_referencia INTEGER,
                lon DOUBLE PRECISION,
                lat DOUBLE PRECISION,
                descargo_codigo TEXT,
                fecha_inidesc TEXT,
                fecha_findesc TEXT,
                cli_plan TEXT,
                estadodesc TEXT,
                fecha_reposicion TEXT,
                direcciones TEXT,
                clientes_afectados INTEGER,
                primera_vez_visto TEXT,
                ultima_vez_visto TEXT,
                activo INTEGER DEFAULT 1,
                fecha_resolucion_detectada TEXT
            );

            CREATE TABLE IF NOT EXISTS descargos_versiones (
                snapshot_ts TEXT,
                numpos TEXT,
                incidencia TEXT,
                estadodesc TEXT,
                fecha_reposicion TEXT,
                clientes_afectados INTEGER,
                direcciones TEXT,
                h3_index TEXT,
                lon DOUBLE PRECISION,
                lat DOUBLE PRECISION
            );

            CREATE INDEX IF NOT EXISTS idx_descargos_hist_numpos ON descargos_versiones(numpos);
            CREATE INDEX IF NOT EXISTS idx_descargos_hist_ts ON descargos_versiones(snapshot_ts);

            -- Feed 4: estado general del sistema (una fila por corrida)
            CREATE TABLE IF NOT EXISTS estado_sistema (
                snapshot_ts TEXT PRIMARY KEY,
                enel_datos TEXT,
                porcentaje INTEGER
            );
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


def _marcar_resueltos_generico(conn, tabla, columna_pk, claves_vistas_hoy, snapshot_ts):
    """RF-10/RNF-06: cualquier fila que estaba activa y dejo de aparecer en
    el feed de esta corrida se marca como resuelta. Reutilizado por
    eventos, trafos_afectados y descargos_programados (mismo patron)."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {columna_pk} FROM {tabla} WHERE activo = 1")
        activos_previos = {r[0] for r in cur.fetchall()}
        recien_resueltos = activos_previos - claves_vistas_hoy

        if recien_resueltos:
            cur.execute(
                f"""
                UPDATE {tabla} SET activo = 0, fecha_resolucion_detectada = %s
                WHERE {columna_pk} = ANY(%s)
                """,
                (snapshot_ts, list(recien_resueltos)),
            )
    return len(recien_resueltos)


def marcar_resueltos(conn, cod_eventos_vistos_hoy, snapshot_ts):
    return _marcar_resueltos_generico(conn, "eventos", "cod_evento", cod_eventos_vistos_hoy, snapshot_ts)


def upsert_trafo(conn, snapshot_ts, numpos, props, h3_index, en_malla_ref, lon, lat,
                  clientes_afectados, direcciones):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trafos_afectados (
                numpos, incidencia, tipo, tension, id_alim, h3_index,
                en_malla_h3_referencia, lon, lat, fecha_inicio,
                estadoinc, fecha_reposicion, direcciones, clientes_afectados,
                primera_vez_visto, ultima_vez_visto, activo
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
            ON CONFLICT (numpos) DO UPDATE SET
                estadoinc = EXCLUDED.estadoinc,
                fecha_reposicion = EXCLUDED.fecha_reposicion,
                direcciones = EXCLUDED.direcciones,
                clientes_afectados = EXCLUDED.clientes_afectados,
                ultima_vez_visto = EXCLUDED.ultima_vez_visto,
                activo = 1,
                fecha_resolucion_detectada = NULL
            """,
            (
                numpos, props.get("INCIDENCIA"), props.get("TIPO"), props.get("TENSION"),
                props.get("id_alim"), h3_index, int(en_malla_ref), lon, lat,
                props.get("FECHA_INICIO"), props.get("ESTADOINC"), props.get("FECHA_REPOSICION"),
                direcciones, clientes_afectados, snapshot_ts, snapshot_ts,
            ),
        )
        cur.execute(
            """
            INSERT INTO trafos_versiones (
                snapshot_ts, numpos, incidencia, estadoinc, fecha_reposicion,
                clientes_afectados, direcciones, h3_index, lon, lat
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                snapshot_ts, numpos, props.get("INCIDENCIA"), props.get("ESTADOINC"),
                props.get("FECHA_REPOSICION"), clientes_afectados, direcciones, h3_index, lon, lat,
            ),
        )


def upsert_descargo(conn, snapshot_ts, numpos, props, h3_index, en_malla_ref, lon, lat,
                     clientes_afectados, direcciones):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO descargos_programados (
                numpos, incidencia, tipo, tension, id_alim, h3_index,
                en_malla_h3_referencia, lon, lat, descargo_codigo, fecha_inidesc,
                fecha_findesc, cli_plan, estadodesc, fecha_reposicion,
                direcciones, clientes_afectados, primera_vez_visto,
                ultima_vez_visto, activo
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
            ON CONFLICT (numpos) DO UPDATE SET
                estadodesc = EXCLUDED.estadodesc,
                fecha_reposicion = EXCLUDED.fecha_reposicion,
                direcciones = EXCLUDED.direcciones,
                clientes_afectados = EXCLUDED.clientes_afectados,
                ultima_vez_visto = EXCLUDED.ultima_vez_visto,
                activo = 1,
                fecha_resolucion_detectada = NULL
            """,
            (
                numpos, props.get("INCIDENCIA"), props.get("TIPO"), props.get("TENSION"),
                props.get("id_alim"), h3_index, int(en_malla_ref), lon, lat,
                props.get("DESCARGO"), props.get("FECHA_INIDESC"), props.get("FECHA_FINDESC"),
                props.get("CLI_PLAN"), props.get("ESTADODESC"), props.get("FECHA_REPOSICION"),
                direcciones, clientes_afectados, snapshot_ts, snapshot_ts,
            ),
        )
        cur.execute(
            """
            INSERT INTO descargos_versiones (
                snapshot_ts, numpos, incidencia, estadodesc, fecha_reposicion,
                clientes_afectados, direcciones, h3_index, lon, lat
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                snapshot_ts, numpos, props.get("INCIDENCIA"), props.get("ESTADODESC"),
                props.get("FECHA_REPOSICION"), clientes_afectados, direcciones, h3_index, lon, lat,
            ),
        )


def insertar_estado_sistema(conn, snapshot_ts, datos, porcentaje):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO estado_sistema (snapshot_ts, enel_datos, porcentaje)
            VALUES (%s,%s,%s)
            ON CONFLICT (snapshot_ts) DO NOTHING
            """,
            (snapshot_ts, datos, porcentaje),
        )


TABLAS_ESPERADAS = (
    "eventos", "historico_versiones",
    "trafos_afectados", "trafos_versiones",
    "descargos_programados", "descargos_versiones",
    "estado_sistema",
)


def verificar_integridad_db(conn):
    """Verificacion simple de integridad al inicio de la corrida: confirma
    que las tablas esperadas existen y son consultables. No reemplaza a
    init_db (que las crea si faltan) - esto es una lectura de diagnostico,
    pensada para detectar temprano un problema de esquema antes de gastar
    tiempo descargando los 4 feeds de Enel."""
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        existentes = {r[0] for r in cur.fetchall()}
    faltantes = [t for t in TABLAS_ESPERADAS if t not in existentes]
    if faltantes:
        logging.warning(
            "Verificacion de integridad: faltan las tablas %s (init_db las creara)", faltantes
        )
    else:
        logging.info("Verificacion de integridad: las %d tablas esperadas existen", len(TABLAS_ESPERADAS))
    return faltantes


def purgar_historico(conn, dias_retencion=None):
    """RNF-07: evita que las tablas de snapshots crudos crezcan sin limite
    corriendo cada 30 min de forma indefinida. Borra de historico_versiones/
    trafos_versiones/descargos_versiones las filas mas viejas que
    `dias_retencion` dias; las tablas de resumen (eventos, trafos_afectados,
    descargos_programados) NO se tocan, conservan su fila actual siempre."""
    if dias_retencion is None:
        dias_retencion = RETENCION_DIAS_HISTORICO
    corte = (datetime.now() - timedelta(days=dias_retencion)).strftime("%Y-%m-%d %H:%M:%S")

    total = 0
    with conn.cursor() as cur:
        for tabla in ("historico_versiones", "trafos_versiones", "descargos_versiones"):
            cur.execute(f"DELETE FROM {tabla} WHERE snapshot_ts < %s", (corte,))
            total += cur.rowcount
    conn.commit()

    if total:
        logging.info(
            "Purga de historico: %d filas eliminadas (retencion %d dias, corte=%s)",
            total, dias_retencion, corte,
        )
    return total


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


def exportar_csv_trafos(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                numpos AS "NumPos",
                incidencia AS "Incidencia",
                tipo AS "Tipo",
                tension AS "Tension",
                id_alim AS "Alimentador",
                h3_index AS "H3Index",
                en_malla_h3_referencia AS "EnMallaH3Referencia",
                lat AS "Latitud",
                lon AS "Longitud",
                clientes_afectados AS "ClientesAfectados",
                direcciones AS "Direcciones",
                estadoinc AS "EstadoIncidencia",
                fecha_inicio AS "FechaInicio",
                fecha_reposicion AS "FechaReposicionEstimada",
                primera_vez_visto AS "PrimeraVezVisto",
                ultima_vez_visto AS "UltimaVezVisto"
            FROM trafos_afectados
            WHERE activo = 1
            ORDER BY ultima_vez_visto DESC
            """
        )
        cols = [d[0] for d in cur.description]
        filas = cur.fetchall()
    _escribir_csv(CSV_TRAFOS_ACTIVOS_PATH, cols, filas)


def _clasificar_descargo(fecha_inidesc_str, fecha_findesc_str, ahora):
    """RF-07: distingue si un descargo programado es futuro, esta en curso,
    o ya finalizo, comparando su ventana horaria contra el momento del
    reporte (no existe distincion de endpoint para esto)."""
    inicio = _parsear_fecha_ini(fecha_inidesc_str)
    fin = _parsear_fecha_ini(fecha_findesc_str)
    if inicio is None or fin is None:
        return None
    if ahora < inicio:
        return "futuro"
    if ahora > fin:
        return "finalizado"
    return "en_curso"


def exportar_csv_descargos(conn):
    ahora = datetime.now()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                numpos AS "NumPos",
                incidencia AS "Incidencia",
                descargo_codigo AS "DescargoCodigo",
                tipo AS "Tipo",
                tension AS "Tension",
                id_alim AS "Alimentador",
                h3_index AS "H3Index",
                en_malla_h3_referencia AS "EnMallaH3Referencia",
                lat AS "Latitud",
                lon AS "Longitud",
                clientes_afectados AS "ClientesAfectados",
                direcciones AS "Direcciones",
                estadodesc AS "EstadoDescargo",
                fecha_inidesc AS "FechaInicioDescargo",
                fecha_findesc AS "FechaFinDescargo",
                fecha_reposicion AS "FechaReposicionEstimada",
                primera_vez_visto AS "PrimeraVezVisto",
                ultima_vez_visto AS "UltimaVezVisto",
                activo AS "Activo"
            FROM descargos_programados
            ORDER BY fecha_inidesc DESC
            """
        )
        cols = [d[0] for d in cur.description]
        idx_inicio = cols.index("FechaInicioDescargo")
        idx_fin = cols.index("FechaFinDescargo")
        filas = []
        for fila in cur.fetchall():
            fila = list(fila)
            fila.append(_clasificar_descargo(fila[idx_inicio], fila[idx_fin], ahora))
            filas.append(fila)
    _escribir_csv(CSV_DESCARGOS_PATH, cols + ["EstadoTemporal"], filas)


def _escribir_csv(path, cols, filas):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(cols)
        writer.writerows(filas)


def copiar_csv_a_onedrive():
    """Copia los CSV ya generados a la carpeta compartida de OneDrive.
    No debe interrumpir la corrida programada si OneDrive esta
    sincronizando y el destino queda momentaneamente bloqueado."""
    for origen in (CSV_ACTIVOS_PATH, CSV_HISTORICO_PATH, CSV_TRAFOS_ACTIVOS_PATH, CSV_DESCARGOS_PATH):
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

    # Verificacion de integridad simple, antes de gastar tiempo descargando
    # los 4 feeds de Enel: confirma que la base local esta accesible y las
    # tablas esperadas existen (init_db las crea si es la primera corrida).
    try:
        conn_check = conectar_db()
        try:
            init_db(conn_check)
            verificar_integridad_db(conn_check)
        finally:
            conn_check.close()
    except Exception as e:
        logging.error("No se pudo verificar la base de datos local antes de iniciar la corrida: %s", e)
        sys.exit(1)

    poligono_comunal = cargar_poligono_comunal()
    malla_h3_referencia = cargar_malla_h3()

    # ------------------------------------------------------------------
    # Feed 1: avisos (critico - si falla, se aborta la corrida como antes)
    # ------------------------------------------------------------------
    try:
        data = descargar_feed()
    except Exception as e:
        logging.error("Error al descargar el feed de avisos: %s", e)
        sys.exit(1)

    features = data.get("features", [])
    logging.info("Total registros en feed de avisos (todo Enel): %d", len(features))

    if not features:
        logging.warning(
            "El feed de avisos llego vacio (0 registros en todo Enel); probable "
            "falla transitoria de la API. No se actualiza el repositorio en esta "
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

    logging.info("Registros de avisos dentro del limite oficial de Las Condes: %d", len(seleccionados))

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

    filas_eventos = _preparar_filas(seleccionados, clientes_por_evento, cod_avisos_por_evento, ids_aviso_por_evento)
    cod_eventos_hoy = {fila[0] for fila in filas_eventos}

    # RF-05: direcciones/clientes de los avisos, indexados por COD_EVENTO/CODIGO,
    # para enriquecer trafosAfectados y descargos via su campo INCIDENCIA
    avisos_por_incidencia = _consolidar_avisos_por_incidencia(seleccionados)

    # ------------------------------------------------------------------
    # Feed 2: transformadores afectados (RNF-02: no bloquea la corrida si falla;
    # `None` significa "no se pudo verificar esta corrida", distinto de una
    # lista vacia (0 trafos afectados en Las Condes en este momento, valido)
    # ------------------------------------------------------------------
    filas_trafos, numpos_trafos_hoy = None, None
    try:
        data_trafos = descargar_trafos()
        features_trafos = data_trafos.get("features", [])
        logging.info("Total registros en feed de trafos afectados (todo Enel): %d", len(features_trafos))
        if not features_trafos:
            raise RuntimeError("feed de trafos afectados vacio (0 registros en todo Enel)")
        seleccionados_trafos = _filtrar_por_comuna_poligonos(
            features_trafos, "TRAFO", poligono_comunal, malla_h3_referencia
        )
        logging.info("Trafos afectados dentro del limite oficial de Las Condes: %d", len(seleccionados_trafos))
        filas_trafos = _preparar_filas_polygon(seleccionados_trafos, avisos_por_incidencia)
        numpos_trafos_hoy = {fila[0] for fila in filas_trafos}
    except Exception as e:
        logging.error("Error en el feed de trafos afectados (se omite esta corrida, no se marca nada como resuelto): %s", e)

    # ------------------------------------------------------------------
    # Feed 3: descargos programados (misma logica de resiliencia que trafos)
    # ------------------------------------------------------------------
    filas_descargos, numpos_descargos_hoy = None, None
    try:
        data_descargos = descargar_descargos()
        features_descargos = data_descargos.get("features", [])
        logging.info("Total registros en feed de descargos (todo Enel): %d", len(features_descargos))
        seleccionados_descargos = _filtrar_por_comuna_poligonos(
            features_descargos, None, poligono_comunal, malla_h3_referencia
        )
        logging.info("Descargos dentro del limite oficial de Las Condes: %d", len(seleccionados_descargos))
        filas_descargos = _preparar_filas_polygon(seleccionados_descargos, avisos_por_incidencia)
        numpos_descargos_hoy = {fila[0] for fila in filas_descargos}
    except Exception as e:
        logging.error("Error en el feed de descargos (se omite esta corrida, no se marca nada como resuelto): %s", e)

    # ------------------------------------------------------------------
    # Feed 4: estado general del sistema (RF-08, sin concepto de activo/resuelto)
    # ------------------------------------------------------------------
    estado_row = None
    try:
        data_estado = descargar_estado()
        estado_row = (data_estado.get("datos"), data_estado.get("porcentaje"))
        logging.info("Estado del sistema Enel: datos=%s porcentaje=%s", *estado_row)
    except Exception as e:
        logging.error("Error en el feed de estado del sistema (se omite esta corrida): %s", e)

    # ------------------------------------------------------------------
    # Escritura: Postgres local (obligatorio) + replica best-effort a Supabase
    # ------------------------------------------------------------------
    conn = conectar_db()
    try:
        n_resueltos, n_resueltos_trafos, n_resueltos_descargos = _aplicar_corrida(
            conn, snapshot_ts, filas_eventos, cod_eventos_hoy,
            filas_trafos, numpos_trafos_hoy,
            filas_descargos, numpos_descargos_hoy,
            estado_row,
        )
        exportar_csv(conn)
        exportar_csv_trafos(conn)
        exportar_csv_descargos(conn)
    finally:
        conn.close()

    _replicar_a_supabase(
        snapshot_ts, filas_eventos, cod_eventos_hoy,
        filas_trafos, numpos_trafos_hoy,
        filas_descargos, numpos_descargos_hoy,
        estado_row,
    )

    copiar_csv_a_onedrive()

    logging.info("Eventos activos en esta corrida: %d", len(cod_eventos_hoy))
    logging.info("Eventos marcados como resueltos en esta corrida: %d", n_resueltos)
    logging.info("Trafos afectados activos en esta corrida: %s", len(numpos_trafos_hoy) if numpos_trafos_hoy is not None else "feed fallo")
    logging.info("Trafos marcados como resueltos en esta corrida: %d", n_resueltos_trafos)
    logging.info("Descargos activos en esta corrida: %s", len(numpos_descargos_hoy) if numpos_descargos_hoy is not None else "feed fallo")
    logging.info("Descargos marcados como resueltos en esta corrida: %d", n_resueltos_descargos)
    logging.info("CSV activos: %s", CSV_ACTIVOS_PATH)
    logging.info("CSV historico: %s", CSV_HISTORICO_PATH)
    logging.info("CSV trafos activos: %s", CSV_TRAFOS_ACTIVOS_PATH)
    logging.info("CSV descargos: %s", CSV_DESCARGOS_PATH)
    logging.info("=== Ejecucion finalizada ===\n")


def _preparar_filas(seleccionados, clientes_por_evento, cod_avisos_por_evento, ids_aviso_por_evento):
    """Calcula, una sola vez, los valores agregados por evento
    (clientes_afectados, avisos_unicos, cod_avisos, ids_aviso) para no
    repetir el calculo al escribir tanto al Postgres local como a Supabase."""
    filas = []
    for feat, h3_index, en_malla_ref, lon, lat in seleccionados:
        props = feat.get("properties", {})
        cod_evento = props.get("COD_EVENTO") or props.get("CODIGO")

        clientes_set = clientes_por_evento.get(cod_evento, set())
        clientes_afectados = len(clientes_set)
        avisos_unicos = ",".join(str(c) for c in sorted(clientes_set, key=str))
        cod_avisos = ",".join(str(c) for c in sorted(cod_avisos_por_evento.get(cod_evento, set()), key=str))
        ids_aviso = ",".join(str(c) for c in sorted(ids_aviso_por_evento.get(cod_evento, set()), key=str))

        filas.append((
            cod_evento, props, h3_index, en_malla_ref, lon, lat,
            clientes_afectados, avisos_unicos, cod_avisos, ids_aviso,
        ))
    return filas


def _escribir_eventos(conn, snapshot_ts, filas, cod_eventos_hoy):
    for (cod_evento, props, h3_index, en_malla_ref, lon, lat,
         clientes_afectados, avisos_unicos, cod_avisos, ids_aviso) in filas:
        upsert_evento(
            conn, snapshot_ts, cod_evento, props, h3_index, en_malla_ref,
            lon, lat, clientes_afectados, avisos_unicos, cod_avisos, ids_aviso,
        )
    return marcar_resueltos(conn, cod_eventos_hoy, snapshot_ts)


def _escribir_trafos(conn, snapshot_ts, filas, numpos_hoy):
    for numpos, props, h3_index, en_malla_ref, lon, lat, clientes_afectados, direcciones in filas:
        upsert_trafo(
            conn, snapshot_ts, numpos, props, h3_index, en_malla_ref,
            lon, lat, clientes_afectados, direcciones,
        )
    return _marcar_resueltos_generico(conn, "trafos_afectados", "numpos", numpos_hoy, snapshot_ts)


def _escribir_descargos(conn, snapshot_ts, filas, numpos_hoy):
    for numpos, props, h3_index, en_malla_ref, lon, lat, clientes_afectados, direcciones in filas:
        upsert_descargo(
            conn, snapshot_ts, numpos, props, h3_index, en_malla_ref,
            lon, lat, clientes_afectados, direcciones,
        )
    return _marcar_resueltos_generico(conn, "descargos_programados", "numpos", numpos_hoy, snapshot_ts)


def _aplicar_corrida(conn, snapshot_ts, filas_eventos, cod_eventos_hoy,
                      filas_trafos, numpos_trafos_hoy,
                      filas_descargos, numpos_descargos_hoy,
                      estado_row):
    """Aplica una corrida completa (los 4 feeds) sobre una conexion (local o
    Supabase). Los feeds cuyas filas sean `None` fallaron esta corrida y se
    omiten sin tocar su tabla (para no marcar como resueltos registros que
    en realidad no se pudieron verificar)."""
    init_db(conn)

    n_resueltos = _escribir_eventos(conn, snapshot_ts, filas_eventos, cod_eventos_hoy)

    n_resueltos_trafos = 0
    if filas_trafos is not None:
        n_resueltos_trafos = _escribir_trafos(conn, snapshot_ts, filas_trafos, numpos_trafos_hoy)

    n_resueltos_descargos = 0
    if filas_descargos is not None:
        n_resueltos_descargos = _escribir_descargos(conn, snapshot_ts, filas_descargos, numpos_descargos_hoy)

    if estado_row is not None:
        insertar_estado_sistema(conn, snapshot_ts, *estado_row)

    conn.commit()

    try:
        purgar_historico(conn)
    except Exception as e:
        logging.error("Fallo la purga de historico (no afecta los datos de esta corrida): %s", e)

    return n_resueltos, n_resueltos_trafos, n_resueltos_descargos


def _replicar_a_supabase(snapshot_ts, filas_eventos, cod_eventos_hoy,
                         filas_trafos, numpos_trafos_hoy,
                         filas_descargos, numpos_descargos_hoy,
                         estado_row):
    """Replica la misma corrida (los 4 feeds) a Supabase, para que Superset
    (u otro visualizador externo) pueda leerla. Es best-effort: si Supabase
    no esta configurado o falla la conexion/escritura, se loggea y se sigue
    sin afectar la corrida local (que ya se guardo antes de llamar a esto)."""
    if not SUPABASE_DB_HOST:
        return

    try:
        conn = conectar_supabase()
    except Exception as e:
        logging.error("No se pudo conectar a Supabase, se omite la replica de esta corrida: %s", e)
        return

    try:
        _aplicar_corrida(
            conn, snapshot_ts, filas_eventos, cod_eventos_hoy,
            filas_trafos, numpos_trafos_hoy,
            filas_descargos, numpos_descargos_hoy,
            estado_row,
        )
        logging.info("Replica a Supabase completada")
    except Exception as e:
        conn.rollback()
        logging.error("Fallo al replicar a Supabase (no afecta la corrida local): %s", e)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
