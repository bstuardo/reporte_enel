"""Tests de la API de solo lectura (enel_las_condes_api.py) contra la
misma base PostgreSQL de prueba (enel_las_condes_test) que usan los tests
del script principal. Nunca toca la base de produccion (enel_las_condes)."""

from datetime import datetime

import psycopg2
import pytest
from fastapi.testclient import TestClient

import enel_las_condes_api as api
import enel_las_condes_historico as historico

TEST_DB_NAME = "enel_las_condes_test"

TEST_DB_CONFIG = {
    "host": historico.DB_HOST,
    "port": historico.DB_PORT,
    "dbname": TEST_DB_NAME,
    "user": historico.DB_USER,
    "password": historico.DB_PASSWORD,
}


class _FakeDatetime(datetime):
    """Fija datetime.now() dentro del modulo de la API para probar
    HorasActivo de forma determinista."""
    _ahora_fija = datetime(2026, 7, 22, 12, 0, 0)

    @classmethod
    def now(cls, tz=None):
        return cls._ahora_fija


def _crear_db():
    conn = psycopg2.connect(**TEST_DB_CONFIG)
    historico.init_db(conn)
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE eventos, historico_versiones, trafos_afectados, trafos_versiones, "
            "descargos_programados, descargos_versiones, estado_sistema"
        )
    conn.commit()

    historico.upsert_evento(
        conn, "2026-07-22 08:00:00", "EVT-1",
        {"CODIGO": "C1", "TIPO": "AVISO", "DIRECCION": "Apoquindo 1000", "FALLA": "Corte programado",
         "DESC_EVENTO": "prueba", "id_alim": "AL-1", "FECHA_INI": "22-07-2026 07:00",
         "FECHA_REPOSICION": "22-07-2026 12:00"},
        "88b2c5199", True, -70.58, -33.41, 3, "1,2,3", "900001,900002,900003", "ID-1,ID-2,ID-3",
    )
    historico.upsert_evento(
        conn, "2026-07-22 08:00:00", "EVT-2",
        {"CODIGO": "C2", "TIPO": "AVISOC", "DIRECCION": "Manquehue 500", "FALLA": "Falla equipo",
         "DESC_EVENTO": "prueba", "id_alim": "AL-2", "FECHA_INI": "22-07-2026 02:00",
         "FECHA_REPOSICION": "22-07-2026 10:00"},
        "88b2c5198", True, -70.56, -33.42, 5, "4,5,6,7,8", "900004", "ID-4",
    )
    conn.commit()
    historico.marcar_resueltos(conn, {"EVT-1"}, "2026-07-22 09:00:00")  # EVT-2 queda resuelto
    conn.commit()

    historico.upsert_trafo(
        conn, "2026-07-27 09:00:00", "NP-1",
        {"TIPO": "TRAFO", "TENSION": "MT", "INCIDENCIA": "DF-T1", "id_alim": "500",
         "FECHA_INICIO": "27-07-2026 08:00", "ESTADOINC": "Activo", "FECHA_REPOSICION": "27-07-2026 13:00"},
        "88b2c5199", True, -70.58, -33.41, 12, "Los Alamos 123",
    )
    conn.commit()

    historico.upsert_descargo(
        conn, "2026-07-27 09:00:00", "ND-1",
        {"TIPO": "DESCARGO", "TENSION": "MT", "INCIDENCIA": "DF-D1", "id_alim": "600",
         "DESCARGO": "DF-D1(TP1)", "FECHA_INIDESC": "22-07-2026 05:00", "FECHA_FINDESC": "22-07-2026 09:00",
         "CLI_PLAN": "1", "ESTADODESC": "789", "FECHA_REPOSICION": "22-07-2026 09:00"},
        "88b2c5198", True, -70.56, -33.42, 4, "Manquehue 500",
    )
    conn.commit()

    historico.insertar_estado_sistema(conn, "2026-07-22 08:00:00", "22/07 08:00", 7)
    conn.commit()
    conn.close()


@pytest.fixture
def client():
    _crear_db()
    api.app.dependency_overrides[api.get_db_config] = lambda: TEST_DB_CONFIG
    with TestClient(api.app) as c:
        yield c
    api.app.dependency_overrides.clear()


def test_health_reporta_db_alcanzable(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db_reachable"] is True


def test_health_reporta_db_no_alcanzable():
    # Puerto sin nada escuchando: produce un error de conexion generado por
    # libpq (ASCII), a diferencia de un dbname invalido cuyo mensaje de error
    # viene del servidor en el locale configurado (Spanish_Chile.1252 en este
    # equipo) y rompe la decodificacion UTF-8 de psycopg2.
    config_invalido = {**TEST_DB_CONFIG, "port": "59999"}
    api.app.dependency_overrides[api.get_db_config] = lambda: config_invalido
    with TestClient(api.app) as c:
        resp = c.get("/health")
    api.app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["db_reachable"] is False


def test_eventos_activos_solo_devuelve_los_activos(client):
    resp = client.get("/eventos/activos")
    assert resp.status_code == 200
    data = resp.json()
    codigos = {row["CodigoEvento"] for row in data}
    assert codigos == {"EVT-1"}
    assert data[0]["ClientesAfectados"] == 3
    assert data[0]["Direccion"] == "Apoquindo 1000"


def test_eventos_activos_incluye_los_campos_agregados(client):
    resp = client.get("/eventos/activos")
    fila = resp.json()[0]
    assert fila["Codigo"] == "C1"
    assert fila["Tipo"] == "AVISO"
    assert fila["DescripcionEvento"] == "prueba"
    assert fila["EnMallaH3Referencia"] == 1
    assert fila["ClientesUnicos"] == "1,2,3"
    assert fila["CodigosAviso"] == "900001,900002,900003"
    assert fila["IdsAviso"] == "ID-1,ID-2,ID-3"


def test_eventos_historico_sin_filtro_devuelve_todos(client):
    resp = client.get("/eventos/historico")
    assert resp.status_code == 200
    codigos = {row["CodigoEvento"] for row in resp.json()}
    assert codigos == {"EVT-1", "EVT-2"}


def test_eventos_historico_filtrado_por_activo_true(client):
    resp = client.get("/eventos/historico", params={"activo": "true"})
    codigos = {row["CodigoEvento"] for row in resp.json()}
    assert codigos == {"EVT-1"}


def test_eventos_historico_filtrado_por_activo_false(client):
    resp = client.get("/eventos/historico", params={"activo": "false"})
    data = resp.json()
    assert {row["CodigoEvento"] for row in data} == {"EVT-2"}
    assert data[0]["Activo"] == 0
    assert data[0]["FechaResolucionDetectada"] == "2026-07-22 09:00:00"


def test_horas_activo_para_evento_activo_cuenta_hasta_ahora(client, monkeypatch):
    monkeypatch.setattr(api, "datetime", _FakeDatetime)
    resp = client.get("/eventos/activos")
    fila = resp.json()[0]
    assert fila["CodigoEvento"] == "EVT-1"
    assert fila["HorasActivo"] == 5.0  # 07:00 -> 12:00 (ahora fija)


def test_horas_activo_para_evento_resuelto_cuenta_hasta_la_resolucion(client, monkeypatch):
    monkeypatch.setattr(api, "datetime", _FakeDatetime)
    resp = client.get("/eventos/historico", params={"activo": "false"})
    fila = resp.json()[0]
    assert fila["CodigoEvento"] == "EVT-2"
    assert fila["HorasActivo"] == 7.0  # 02:00 -> 09:00 (resolucion), no hasta las 12:00


def test_evento_versiones_devuelve_snapshots_del_evento(client):
    resp = client.get("/eventos/EVT-1/versiones")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["CodigoEvento"] == "EVT-1"
    assert data[0]["Direccion"] == "Apoquindo 1000"


def test_evento_versiones_evento_inexistente_devuelve_lista_vacia(client):
    resp = client.get("/eventos/NO-EXISTE/versiones")
    assert resp.status_code == 200
    assert resp.json() == []


def test_health_con_dbname_invalido_no_revienta_por_unicodedecodeerror():
    """Caso real encontrado en este equipo: el mensaje de error que Postgres
    devuelve por un nombre de base invalido viene en el locale del servidor
    (Spanish_Chile.1252, con comillas angulares acentuadas), y psycopg2
    puede fallar al decodificarlo como UTF-8 (UnicodeDecodeError en vez de
    psycopg2.OperationalError). /health debe seguir respondiendo 200 con
    db_reachable=False, no un 500 crudo."""
    config_invalido = {**TEST_DB_CONFIG, "dbname": "base_que_no_existe"}
    api.app.dependency_overrides[api.get_db_config] = lambda: config_invalido
    with TestClient(api.app) as c:
        resp = c.get("/health")
    api.app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["db_reachable"] is False


def test_eventos_activos_sin_base_de_datos_devuelve_503():
    config_invalido = {**TEST_DB_CONFIG, "port": "59999"}
    api.app.dependency_overrides[api.get_db_config] = lambda: config_invalido
    with TestClient(api.app) as c:
        resp = c.get("/eventos/activos")
    api.app.dependency_overrides.clear()
    assert resp.status_code == 503


# ----------------------------------------------------------------------
# Feed 2/3/4: trafos, descargos, estado
# ----------------------------------------------------------------------

def test_trafos_activos_devuelve_los_datos_esperados(client):
    resp = client.get("/trafos/activos")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    fila = data[0]
    assert fila["NumPos"] == "NP-1"
    assert fila["Incidencia"] == "DF-T1"
    assert fila["Direcciones"] == "Los Alamos 123"
    assert fila["ClientesAfectados"] == 12
    assert fila["EstadoIncidencia"] == "Activo"


def test_descargos_sin_filtro_devuelve_todos(client):
    resp = client.get("/descargos")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["NumPos"] == "ND-1"
    assert data[0]["DescargoCodigo"] == "DF-D1(TP1)"


def test_descargos_filtrado_por_activo(client):
    resp = client.get("/descargos", params={"activo": "true"})
    assert {d["NumPos"] for d in resp.json()} == {"ND-1"}
    resp_falso = client.get("/descargos", params={"activo": "false"})
    assert resp_falso.json() == []


def test_descargos_incluye_estado_temporal_calculado(client, monkeypatch):
    monkeypatch.setattr(api, "datetime", _FakeDatetime)  # ahora fija: 2026-07-22 12:00:00
    resp = client.get("/descargos")
    fila = resp.json()[0]
    assert fila["EstadoTemporal"] == "finalizado"  # 05:00-09:00 ya paso respecto a las 12:00


def test_estado_devuelve_la_corrida_mas_reciente_primero(client):
    resp = client.get("/estado")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["Datos"] == "22/07 08:00"
    assert data[0]["Porcentaje"] == 7


def test_estado_respeta_el_limite(client):
    resp = client.get("/estado", params={"limit": 1})
    assert resp.status_code == 200
    assert len(resp.json()) <= 1
