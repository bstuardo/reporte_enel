"""
API de solo lectura sobre el repositorio historico de Enel Las Condes
(PostgreSQL), para conectar Power BI u otro visualizador sin depender de
los archivos CSV.

No escribe en la base de datos: enel_las_condes_historico.py sigue
siendo el unico proceso que la actualiza (via Task Scheduler cada
10-15 min). Esta API abre cada conexion en modo read-only.

Conexion a PostgreSQL (variables de entorno, con default entre parentesis):
    ENEL_DB_HOST     (localhost)
    ENEL_DB_PORT     (5432)
    ENEL_DB_NAME     (enel_las_condes)
    ENEL_DB_USER     (postgres)
    ENEL_DB_PASSWORD (vacio)

Correr localmente:
    uvicorn enel_las_condes_api:app --host 0.0.0.0 --port 8000

Endpoints:
    GET /health                          -> estado del servicio y de la BD
    GET /eventos/activos                 -> avisos activos (igual que el CSV de activos)
    GET /eventos/historico                -> avisos, historico completo (igual que el CSV historico)
                                              ?activo=true|false para filtrar
    GET /eventos/{cod_evento}/versiones  -> snapshots historicos de un aviso puntual
    GET /trafos/activos                  -> transformadores afectados activos (feed 2)
    GET /descargos                       -> descargos programados (feed 3)
                                             ?activo=true|false para filtrar
    GET /estado                          -> historico del health-check de Enel (feed 4)
                                             ?limit=N (default 100)
"""

import os
from datetime import datetime
from typing import List, Optional

import psycopg2
import psycopg2.extras
from fastapi import Depends, FastAPI, HTTPException, Query

DB_HOST = os.environ.get("ENEL_DB_HOST", "localhost")
DB_PORT = os.environ.get("ENEL_DB_PORT", "5432")
DB_NAME = os.environ.get("ENEL_DB_NAME", "enel_las_condes")
DB_USER = os.environ.get("ENEL_DB_USER", "postgres")
DB_PASSWORD = os.environ.get("ENEL_DB_PASSWORD", "")

app = FastAPI(
    title="Enel Las Condes - API de eventos",
    description=(
        "Repositorio historico de cortes Enel filtrados para la comuna de "
        "Las Condes, generado por enel_las_condes_historico.py."
    ),
    version="2.0.0",
)


def get_db_config() -> dict:
    return {"host": DB_HOST, "port": DB_PORT, "dbname": DB_NAME, "user": DB_USER, "password": DB_PASSWORD}


def _conectar(db_config: dict):
    try:
        return psycopg2.connect(
            cursor_factory=psycopg2.extras.RealDictCursor,
            options="-c default_transaction_read_only=on",
            **db_config,
        )
    except (psycopg2.Error, UnicodeDecodeError) as e:
        # UnicodeDecodeError puede ocurrir ademas de psycopg2.OperationalError:
        # si el servidor rechaza la conexion (ej. nombre de base invalido), el
        # mensaje de error viene en el locale del servidor (Spanish_Chile.1252
        # en este equipo) y psycopg2 puede fallar al decodificarlo como UTF-8.
        raise HTTPException(status_code=503, detail=f"No se pudo conectar a la base de datos: {e}")


def _parsear_fecha_ini(valor):
    """FechaInicio viene de Enel como 'DD-MM-YYYY HH:MM'."""
    if not valor:
        return None
    try:
        return datetime.strptime(valor, "%d-%m-%Y %H:%M")
    except ValueError:
        return None


def _parsear_snapshot(valor):
    """PrimeraVezVisto/FechaResolucionDetectada usan 'YYYY-MM-DD HH:MM:SS'."""
    if not valor:
        return None
    try:
        return datetime.strptime(valor, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _horas_activo(fecha_ini_str, fin_dt):
    """Horas entre FechaInicio y `fin_dt` (ahora para eventos activos, o la
    fecha de resolucion detectada para los ya resueltos)."""
    fecha_ini = _parsear_fecha_ini(fecha_ini_str)
    if fecha_ini is None or fin_dt is None:
        return None
    return round((fin_dt - fecha_ini).total_seconds() / 3600, 1)


def _clasificar_descargo(fecha_inidesc_str, fecha_findesc_str, ahora):
    """RF-07: futuro / en_curso / finalizado, comparando la ventana horaria
    del descargo programado contra el momento de la consulta."""
    inicio = _parsear_fecha_ini(fecha_inidesc_str)
    fin = _parsear_fecha_ini(fecha_findesc_str)
    if inicio is None or fin is None:
        return None
    if ahora < inicio:
        return "futuro"
    if ahora > fin:
        return "finalizado"
    return "en_curso"


ACTIVOS_SQL = """
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

HISTORICO_SQL_BASE = """
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
"""

VERSIONES_SQL = """
    SELECT
        snapshot_ts AS "SnapshotTs",
        cod_evento AS "CodigoEvento",
        direccion AS "Direccion",
        falla AS "DetalleFalla",
        id_alim AS "Alimentador",
        h3_index AS "H3Index",
        lat AS "Latitud",
        lon AS "Longitud",
        fecha_ini AS "FechaInicio",
        fecha_reposicion_estimada AS "FechaReposicionEstimada",
        clientes_afectados AS "ClientesAfectados"
    FROM historico_versiones
    WHERE cod_evento = %s
    ORDER BY snapshot_ts
"""

TRAFOS_ACTIVOS_SQL = """
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

DESCARGOS_SQL_BASE = """
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
"""

ESTADO_SQL = """
    SELECT
        snapshot_ts AS "SnapshotTs",
        enel_datos AS "Datos",
        porcentaje AS "Porcentaje"
    FROM estado_sistema
    ORDER BY snapshot_ts DESC
    LIMIT %s
"""


@app.get("/")
def raiz():
    return {
        "servicio": "Enel Las Condes - API de eventos",
        "docs": "/docs",
        "endpoints": [
            "/health",
            "/eventos/activos",
            "/eventos/historico",
            "/eventos/{cod_evento}/versiones",
            "/trafos/activos",
            "/descargos",
            "/estado",
        ],
    }


@app.get("/health")
def health(db_config: dict = Depends(get_db_config)):
    try:
        conn = psycopg2.connect(options="-c default_transaction_read_only=on", **db_config)
        conn.close()
        return {
            "status": "ok",
            "db_host": db_config["host"],
            "db_name": db_config["dbname"],
            "db_reachable": True,
        }
    except (psycopg2.Error, UnicodeDecodeError) as e:
        return {
            "status": "ok",
            "db_host": db_config["host"],
            "db_name": db_config["dbname"],
            "db_reachable": False,
            "error": str(e),
        }


@app.get("/eventos/activos")
def eventos_activos(db_config: dict = Depends(get_db_config)) -> List[dict]:
    ahora = datetime.now()
    conn = _conectar(db_config)
    try:
        with conn.cursor() as cur:
            cur.execute(ACTIVOS_SQL)
            filas = cur.fetchall()
        resultado = []
        for fila in filas:
            fila = dict(fila)
            fila["HorasActivo"] = _horas_activo(fila["FechaInicio"], ahora)
            resultado.append(fila)
        return resultado
    finally:
        conn.close()


@app.get("/eventos/historico")
def eventos_historico(
    activo: Optional[bool] = Query(None, description="Filtrar por activo=true o activo=false"),
    db_config: dict = Depends(get_db_config),
) -> List[dict]:
    sql = HISTORICO_SQL_BASE
    params: list = []
    if activo is not None:
        sql += " WHERE activo = %s"
        params.append(1 if activo else 0)
    sql += " ORDER BY primera_vez_visto DESC"

    ahora = datetime.now()
    conn = _conectar(db_config)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            filas = cur.fetchall()
        resultado = []
        for fila in filas:
            fila = dict(fila)
            fin = ahora if fila["Activo"] else (_parsear_snapshot(fila["FechaResolucionDetectada"]) or ahora)
            fila["HorasActivo"] = _horas_activo(fila["FechaInicio"], fin)
            resultado.append(fila)
        return resultado
    finally:
        conn.close()


@app.get("/eventos/{cod_evento}/versiones")
def evento_versiones(cod_evento: str, db_config: dict = Depends(get_db_config)) -> List[dict]:
    conn = _conectar(db_config)
    try:
        with conn.cursor() as cur:
            cur.execute(VERSIONES_SQL, (cod_evento,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


@app.get("/trafos/activos")
def trafos_activos(db_config: dict = Depends(get_db_config)) -> List[dict]:
    conn = _conectar(db_config)
    try:
        with conn.cursor() as cur:
            cur.execute(TRAFOS_ACTIVOS_SQL)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


@app.get("/descargos")
def descargos(
    activo: Optional[bool] = Query(None, description="Filtrar por activo=true o activo=false"),
    db_config: dict = Depends(get_db_config),
) -> List[dict]:
    sql = DESCARGOS_SQL_BASE
    params: list = []
    if activo is not None:
        sql += " WHERE activo = %s"
        params.append(1 if activo else 0)
    sql += " ORDER BY fecha_inidesc DESC"

    ahora = datetime.now()
    conn = _conectar(db_config)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            filas = cur.fetchall()
        resultado = []
        for fila in filas:
            fila = dict(fila)
            fila["EstadoTemporal"] = _clasificar_descargo(
                fila["FechaInicioDescargo"], fila["FechaFinDescargo"], ahora
            )
            resultado.append(fila)
        return resultado
    finally:
        conn.close()


@app.get("/estado")
def estado_sistema(
    limit: int = Query(100, ge=1, le=1000, description="Cantidad maxima de corridas a devolver"),
    db_config: dict = Depends(get_db_config),
) -> List[dict]:
    conn = _conectar(db_config)
    try:
        with conn.cursor() as cur:
            cur.execute(ESTADO_SQL, (limit,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
