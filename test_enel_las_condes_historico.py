"""
Tests para enel_las_condes_historico.py

Corren contra una base PostgreSQL de prueba separada (enel_las_condes_test),
NUNCA contra enel_las_condes (produccion). Requiere las mismas variables de
entorno ENEL_DB_* que el script real (host/user/password); solo el nombre
de base se fuerza a la de test.

Cubren:
  - carga del poligono comunal oficial desde Limite_Comunal_LasCondes.geojson
    y el filtro point-in-polygon inclusivo (covers: dentro o toca el borde)
  - carga de la malla H3 de referencia y calculo de h3_index
  - logica de la base de datos historica (upsert / marcar_resueltos / export)
  - migracion de esquema (ADD COLUMN IF NOT EXISTS) sin perder datos
  - los dos casos de riesgo corregidos:
      * feed vacio no debe marcar eventos activos como resueltos
      * registros sin COD_EVENTO/CODIGO no deben colisionar en la tabla
"""

import csv
from datetime import datetime, timedelta

import pytest
from shapely.geometry import Point

import enel_las_condes_historico as mod

TEST_DB_NAME = "enel_las_condes_test"


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _usar_db_de_test(monkeypatch):
    """Todas las pruebas de este archivo apuntan a enel_las_condes_test,
    nunca a la base de produccion, incluso si algo llama a mod.conectar_db()."""
    monkeypatch.setattr(mod, "DB_NAME", TEST_DB_NAME)


@pytest.fixture(scope="module")
def poligono():
    return mod.cargar_poligono_comunal()


@pytest.fixture(scope="module")
def punto_dentro(poligono):
    """Punto garantizado dentro del poligono (representative_point)."""
    p = poligono.representative_point()
    return p.x, p.y  # lon, lat


@pytest.fixture(scope="module")
def punto_borde(poligono):
    """Punto exactamente sobre el borde del poligono (primer vertice del anillo exterior)."""
    x, y = poligono.exterior.coords[0]
    return x, y  # lon, lat


@pytest.fixture(scope="module")
def geometria_polygon_dentro(punto_dentro):
    """Geometria GeoJSON Polygon (para simular trafosAfectados/descargos, que
    vienen como Polygon en vez de Point) cuyo representative_point cae
    garantizado dentro del limite comunal."""
    lon, lat = punto_dentro
    d = 0.0005
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon - d, lat - d], [lon + d, lat - d],
            [lon + d, lat + d], [lon - d, lat + d],
            [lon - d, lat - d],
        ]],
    }


@pytest.fixture(scope="module")
def geometria_polygon_fuera():
    """Poligono en pleno Oceano Pacifico, claramente fuera de cualquier comuna."""
    return {
        "type": "Polygon",
        "coordinates": [[
            [-75.0, -33.4], [-74.999, -33.4], [-74.999, -33.399], [-75.0, -33.399], [-75.0, -33.4],
        ]],
    }


# ----------------------------------------------------------------------
# Poligono comunal
# ----------------------------------------------------------------------

def test_cargar_poligono_comunal_es_valido(poligono):
    assert poligono.is_valid
    assert poligono.area > 0


def test_poligono_contiene_un_punto_interno_garantizado(poligono, punto_dentro):
    lon, lat = punto_dentro
    assert poligono.contains(Point(lon, lat))
    assert poligono.covers(Point(lon, lat))


def test_poligono_no_contiene_punto_lejano_fuera_de_santiago(poligono):
    # Punto en pleno Oceano Pacifico, claramente fuera de cualquier comuna
    assert not poligono.covers(Point(-75.0, -33.4))


def test_covers_incluye_puntos_que_solo_tocan_el_borde(poligono, punto_borde):
    """El filtro usado en main() es covers(), no contains(): un punto
    justo sobre el limite comunal debe pasar el filtro igual."""
    lon, lat = punto_borde
    punto = Point(lon, lat)
    assert poligono.covers(punto)
    # contains() en cambio es estricto y excluye el borde (motivo del cambio)
    assert not poligono.contains(punto)


def test_cargar_poligono_comunal_falla_si_no_existe_la_comuna(monkeypatch):
    monkeypatch.setattr(mod, "NOMBRE_COMUNA_FILTRO", "Comuna Que No Existe")
    with pytest.raises(RuntimeError):
        mod.cargar_poligono_comunal()


# ----------------------------------------------------------------------
# Malla H3
# ----------------------------------------------------------------------

def test_cargar_malla_h3_devuelve_set_no_vacio():
    malla = mod.cargar_malla_h3()
    assert isinstance(malla, set)
    assert len(malla) > 0


def test_h3_de_punto_resolucion_correcta(punto_dentro):
    lon, lat = punto_dentro
    h3_index = mod.h3_de_punto(lat, lon)
    assert h3_index is not None
    import h3 as h3lib
    assert h3lib.get_resolution(h3_index) == mod.H3_RESOLUCION


def test_h3_de_punto_dentro_esta_en_malla_de_referencia(punto_dentro):
    malla = mod.cargar_malla_h3()
    lon, lat = punto_dentro
    h3_index = mod.h3_de_punto(lat, lon)
    assert h3_index in malla


# ----------------------------------------------------------------------
# Base de datos historica (PostgreSQL de prueba, se trunca antes de cada test)
# ----------------------------------------------------------------------

def _fetchone(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _fetchall(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _execute(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)


@pytest.fixture
def conn():
    c = mod.conectar_db()
    mod.init_db(c)
    _execute(c, "TRUNCATE eventos, historico_versiones, trafos_afectados, trafos_versiones, descargos_programados, descargos_versiones, estado_sistema, dim_h3")
    c.commit()
    yield c
    c.close()


def _props(**kwargs):
    base = {
        "CODIGO": "C1", "TIPO": "AVISO", "DIRECCION": "Av. Apoquindo 1234",
        "FALLA": "Corte programado", "DESC_EVENTO": "desc", "id_alim": "AL-1",
        "FECHA_INI": "21-07-2026 10:00", "FECHA_REPOSICION": "21-07-2026 14:00",
        "COD_AVISO": 900001, "ID_AVISO": "ID-AAA",
    }
    base.update(kwargs)
    return base


def test_upsert_evento_inserta_nuevo(conn):
    mod.upsert_evento(
        conn, "2026-07-21 10:00:00", "EVT-1", _props(), "h3abc", True,
        -70.60, -33.41, 3, "111,222,333", "900001", "ID-AAA",
    )
    conn.commit()
    row = _fetchone(conn, "SELECT activo, clientes_afectados FROM eventos WHERE cod_evento='EVT-1'")
    assert row == (1, 3)
    hist = _fetchone(conn, "SELECT COUNT(*) FROM historico_versiones WHERE cod_evento='EVT-1'")[0]
    assert hist == 1


def test_upsert_evento_guarda_tipo_codigo_y_avisos(conn):
    mod.upsert_evento(
        conn, "2026-07-21 10:00:00", "EVT-1", _props(CODIGO="C1", TIPO="AVISOC"), "h3abc", True,
        -70.60, -33.41, 2, "111,222", "900001,900002", "ID-AAA,ID-BBB",
    )
    conn.commit()
    row = _fetchone(
        conn,
        "SELECT codigo, tipo, desc_evento, en_malla_h3_referencia, avisos_unicos, cod_avisos, ids_aviso "
        "FROM eventos WHERE cod_evento='EVT-1'",
    )
    assert row == ("C1", "AVISOC", "desc", 1, "111,222", "900001,900002", "ID-AAA,ID-BBB")


def test_upsert_evento_actualiza_existente_y_agrega_version_historica(conn):
    mod.upsert_evento(conn, "2026-07-21 10:00:00", "EVT-1", _props(), "h3abc", True, -70.60, -33.41, 3, "1,2,3", "900001", "ID-AAA")
    mod.upsert_evento(conn, "2026-07-21 10:15:00", "EVT-1", _props(FALLA="Falla equipo"), "h3abc", True, -70.60, -33.41, 5, "1,2,3,4,5", "900001,900002", "ID-AAA,ID-BBB")
    conn.commit()

    row = _fetchone(
        conn,
        "SELECT falla, clientes_afectados, primera_vez_visto, ultima_vez_visto, cod_avisos, ids_aviso "
        "FROM eventos WHERE cod_evento='EVT-1'",
    )
    assert row == ("Falla equipo", 5, "2026-07-21 10:00:00", "2026-07-21 10:15:00", "900001,900002", "ID-AAA,ID-BBB")

    n_eventos = _fetchone(conn, "SELECT COUNT(*) FROM eventos")[0]
    assert n_eventos == 1  # no duplico la fila, la actualizo (ON CONFLICT DO UPDATE)

    n_hist = _fetchone(conn, "SELECT COUNT(*) FROM historico_versiones WHERE cod_evento='EVT-1'")[0]
    assert n_hist == 2  # pero si dejo dos snapshots en el historico


def test_marcar_resueltos_detecta_evento_que_desaparecio(conn):
    mod.upsert_evento(conn, "t1", "EVT-1", _props(), "h3a", True, -70.6, -33.4, 1, "1", "900001", "ID-AAA")
    mod.upsert_evento(conn, "t1", "EVT-2", _props(), "h3b", True, -70.6, -33.4, 1, "2", "900002", "ID-BBB")
    conn.commit()

    # en la corrida siguiente solo se ve EVT-1 -> EVT-2 se resolvio
    n = mod.marcar_resueltos(conn, {"EVT-1"}, "t2")
    conn.commit()

    assert n == 1
    activo_evt2 = _fetchone(conn, "SELECT activo FROM eventos WHERE cod_evento='EVT-2'")[0]
    assert activo_evt2 == 0
    activo_evt1 = _fetchone(conn, "SELECT activo FROM eventos WHERE cod_evento='EVT-1'")[0]
    assert activo_evt1 == 1


def test_marcar_resueltos_no_hace_nada_si_todo_sigue_activo(conn):
    mod.upsert_evento(conn, "t1", "EVT-1", _props(), "h3a", True, -70.6, -33.4, 1, "1", "900001", "ID-AAA")
    conn.commit()
    n = mod.marcar_resueltos(conn, {"EVT-1"}, "t2")
    assert n == 0


def test_exportar_csv_escribe_solo_activos_y_todo_el_historico(conn, tmp_path, monkeypatch):
    activos_path = tmp_path / "activos.csv"
    historico_path = tmp_path / "historico.csv"
    monkeypatch.setattr(mod, "CSV_ACTIVOS_PATH", activos_path)
    monkeypatch.setattr(mod, "CSV_HISTORICO_PATH", historico_path)

    mod.upsert_evento(conn, "t1", "EVT-1", _props(), "h3a", True, -70.6, -33.4, 1, "1", "900001", "ID-AAA")
    mod.upsert_evento(conn, "t1", "EVT-2", _props(), "h3b", True, -70.6, -33.4, 1, "2", "900002", "ID-BBB")
    conn.commit()
    mod.marcar_resueltos(conn, {"EVT-1"}, "t2")  # EVT-2 queda resuelto
    conn.commit()

    mod.exportar_csv(conn)

    activos_txt = activos_path.read_text(encoding="utf-8-sig")
    assert "EVT-1" in activos_txt
    assert "EVT-2" not in activos_txt

    historico_txt = historico_path.read_text(encoding="utf-8-sig")
    assert "EVT-1" in historico_txt
    assert "EVT-2" in historico_txt  # el historico si conserva los resueltos


def test_exportar_csv_incluye_los_campos_agregados(conn, tmp_path, monkeypatch):
    activos_path = tmp_path / "activos.csv"
    historico_path = tmp_path / "historico.csv"
    monkeypatch.setattr(mod, "CSV_ACTIVOS_PATH", activos_path)
    monkeypatch.setattr(mod, "CSV_HISTORICO_PATH", historico_path)

    mod.upsert_evento(
        conn, "t1", "EVT-1", _props(TIPO="AVISOC", DESC_EVENTO="prueba"), "h3a", True,
        -70.6, -33.4, 2, "111,222", "900001,900002", "ID-AAA,ID-BBB",
    )
    conn.commit()

    mod.exportar_csv(conn)

    activos_txt = activos_path.read_text(encoding="utf-8-sig")
    cabecera = activos_txt.splitlines()[0]
    for columna in ("Codigo", "Tipo", "DescripcionEvento", "EnMallaH3Referencia", "ClientesUnicos", "CodigosAviso", "IdsAviso"):
        assert columna in cabecera
    assert "AVISOC" in activos_txt
    assert "900001,900002" in activos_txt
    assert "ID-AAA,ID-BBB" in activos_txt


# ----------------------------------------------------------------------
# HorasActivo
# ----------------------------------------------------------------------

def test_horas_activo_calcula_diferencia_en_horas():
    fin = datetime(2026, 7, 24, 12, 0, 0)
    assert mod._horas_activo("24-07-2026 06:00", fin) == 6.0
    assert mod._horas_activo("23-07-2026 12:00", fin) == 24.0


def test_horas_activo_none_si_fecha_ini_invalida_o_ausente():
    fin = datetime(2026, 7, 24, 12, 0, 0)
    assert mod._horas_activo(None, fin) is None
    assert mod._horas_activo("", fin) is None
    assert mod._horas_activo("formato-invalido", fin) is None


def test_parsear_snapshot_formato_correcto():
    assert mod._parsear_snapshot("2026-07-24 09:30:00") == datetime(2026, 7, 24, 9, 30, 0)
    assert mod._parsear_snapshot(None) is None
    assert mod._parsear_snapshot("no es una fecha") is None


class _FakeDatetime(datetime):
    """Permite fijar datetime.now() dentro del modulo para probar
    HorasActivo de forma determinista, sin depender del reloj real."""
    _ahora_fija = datetime(2026, 7, 24, 12, 0, 0)

    @classmethod
    def now(cls, tz=None):
        return cls._ahora_fija


def test_exportar_csv_calcula_horas_activo_para_activos_y_resueltos(conn, tmp_path, monkeypatch):
    activos_path = tmp_path / "activos.csv"
    historico_path = tmp_path / "historico.csv"
    monkeypatch.setattr(mod, "CSV_ACTIVOS_PATH", activos_path)
    monkeypatch.setattr(mod, "CSV_HISTORICO_PATH", historico_path)
    monkeypatch.setattr(mod, "datetime", _FakeDatetime)

    # EVT-ACTIVO: inicio 6 horas antes de "ahora", sigue activo
    mod.upsert_evento(
        conn, "t1", "EVT-ACTIVO", _props(FECHA_INI="24-07-2026 06:00"), "h3a", True,
        -70.6, -33.4, 1, "1", "900001", "ID-AAA",
    )
    # EVT-RESUELTO: inicio 10 horas antes de "ahora", se resuelve 4 horas antes de "ahora"
    mod.upsert_evento(
        conn, "t1", "EVT-RESUELTO", _props(FECHA_INI="24-07-2026 02:00"), "h3b", True,
        -70.6, -33.4, 1, "2", "900002", "ID-BBB",
    )
    conn.commit()
    _execute(
        conn,
        "UPDATE eventos SET activo = 0, fecha_resolucion_detectada = %s WHERE cod_evento = 'EVT-RESUELTO'",
        ("2026-07-24 08:00:00",),
    )
    conn.commit()

    mod.exportar_csv(conn)

    historico_txt = historico_path.read_text(encoding="utf-8-sig")
    filas = {r["CodigoEvento"]: r for r in csv.DictReader(historico_txt.splitlines(), delimiter=";")}

    assert filas["EVT-ACTIVO"]["HorasActivo"] == "6.0"  # 06:00 -> 12:00 (ahora)
    assert filas["EVT-RESUELTO"]["HorasActivo"] == "6.0"  # 02:00 -> 08:00 (resolucion), no hasta ahora


# ----------------------------------------------------------------------
# Migracion de esquema (tabla creada por una version anterior del script)
# ----------------------------------------------------------------------

def test_migracion_agrega_columnas_sin_perder_datos_existentes():
    c = mod.conectar_db()
    try:
        with c.cursor() as cur:
            # CASCADE: vw_cortes_unificado/vw_trafos_con_aviso/vw_descargos_con_aviso
            # dependen de "eventos", si existen de una corrida anterior de tests
            cur.execute("DROP TABLE IF EXISTS eventos, historico_versiones CASCADE")
            cur.execute(
                """
                CREATE TABLE eventos (
                    cod_evento TEXT PRIMARY KEY,
                    codigo TEXT,
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
                    activo INTEGER DEFAULT 1,
                    fecha_resolucion_detectada TEXT
                )
                """
            )
            cur.execute(
                "INSERT INTO eventos (cod_evento, direccion, activo) VALUES (%s, %s, %s)",
                ("EVT-VIEJO", "Direccion vieja", 1),
            )
        c.commit()

        mod.init_db(c)  # simula la corrida siguiente, ya con el codigo nuevo

        with c.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'eventos'"
            )
            columnas = {r[0] for r in cur.fetchall()}
        assert {"tipo", "cod_avisos", "ids_aviso"} <= columnas

        row = _fetchone(
            c, "SELECT direccion, tipo, cod_avisos, ids_aviso FROM eventos WHERE cod_evento='EVT-VIEJO'"
        )
        assert row == ("Direccion vieja", None, None, None)  # dato viejo intacto, columnas nuevas en NULL
    finally:
        mod.init_db(c)  # deja el esquema completo listo para el resto de los tests
        mod.crear_vistas(c)  # las vistas se perdieron con el CASCADE, se recrean
        c.close()


# ----------------------------------------------------------------------
# main(): casos de riesgo corregidos
# ----------------------------------------------------------------------

def _preparar_main(monkeypatch, tmp_path, feed):
    monkeypatch.setattr(mod, "CSV_ACTIVOS_PATH", tmp_path / "activos.csv")
    monkeypatch.setattr(mod, "CSV_HISTORICO_PATH", tmp_path / "historico.csv")
    monkeypatch.setattr(mod, "ONEDRIVE_DIR", tmp_path / "onedrive_falso")
    (tmp_path / "onedrive_falso").mkdir()
    monkeypatch.setattr(mod, "descargar_feed", lambda: feed)

    c = mod.conectar_db()
    mod.init_db(c)
    _execute(c, "TRUNCATE eventos, historico_versiones, trafos_afectados, trafos_versiones, descargos_programados, descargos_versiones, estado_sistema, dim_h3")
    c.commit()
    c.close()


def test_main_sale_con_codigo_de_error_si_falla_la_descarga(monkeypatch, tmp_path):
    """El .bat que dispara la tarea programada usa el codigo de salida para
    decidir si loggea la corrida como OK o como ERROR; una falla de red no
    debe terminar en un exit code 0 silencioso."""
    monkeypatch.setattr(mod, "CSV_ACTIVOS_PATH", tmp_path / "activos.csv")
    monkeypatch.setattr(mod, "CSV_HISTORICO_PATH", tmp_path / "historico.csv")
    monkeypatch.setattr(mod, "ONEDRIVE_DIR", tmp_path / "onedrive_falso")

    def _falla():
        raise RuntimeError("timeout simulado")

    monkeypatch.setattr(mod, "descargar_feed", _falla)

    with pytest.raises(SystemExit) as exc_info:
        mod.main()
    assert exc_info.value.code != 0


def test_main_con_feed_vacio_no_marca_nada_como_resuelto(monkeypatch, tmp_path):
    _preparar_main(monkeypatch, tmp_path, {"features": []})

    # precargamos un evento activo directamente en la bd de prueba
    c = mod.conectar_db()
    mod.upsert_evento(c, "t0", "EVT-PREVIO", _props(), "h3a", True, -70.6, -33.4, 1, "1", "900001", "ID-AAA")
    c.commit()
    c.close()

    mod.main()

    c = mod.conectar_db()
    activo = _fetchone(c, "SELECT activo FROM eventos WHERE cod_evento='EVT-PREVIO'")[0]
    c.close()
    assert activo == 1  # sigue activo: un feed vacio no debe "resolverlo"


def test_copiar_csv_a_onedrive_copia_ambos_archivos(monkeypatch, tmp_path):
    activos = tmp_path / "activos.csv"
    historico = tmp_path / "historico.csv"
    activos.write_text("contenido activos", encoding="utf-8-sig")
    historico.write_text("contenido historico", encoding="utf-8-sig")

    destino = tmp_path / "onedrive_falso"
    destino.mkdir()

    monkeypatch.setattr(mod, "CSV_ACTIVOS_PATH", activos)
    monkeypatch.setattr(mod, "CSV_HISTORICO_PATH", historico)
    monkeypatch.setattr(mod, "ONEDRIVE_DIR", destino)

    mod.copiar_csv_a_onedrive()

    assert (destino / "activos.csv").read_text(encoding="utf-8-sig") == "contenido activos"
    assert (destino / "historico.csv").read_text(encoding="utf-8-sig") == "contenido historico"


def test_copiar_csv_a_onedrive_no_lanza_si_destino_no_existe(monkeypatch, tmp_path):
    activos = tmp_path / "activos.csv"
    historico = tmp_path / "historico.csv"
    activos.write_text("x", encoding="utf-8-sig")
    historico.write_text("x", encoding="utf-8-sig")

    monkeypatch.setattr(mod, "CSV_ACTIVOS_PATH", activos)
    monkeypatch.setattr(mod, "CSV_HISTORICO_PATH", historico)
    monkeypatch.setattr(mod, "ONEDRIVE_DIR", tmp_path / "carpeta_que_no_existe")

    mod.copiar_csv_a_onedrive()  # no debe lanzar excepcion, solo loggear el error


def test_main_descarta_features_sin_cod_evento(monkeypatch, tmp_path, punto_dentro):
    lon, lat = punto_dentro
    feed = {
        "features": [
            {
                "geometry": {"coordinates": [lon, lat]},
                "properties": {**_props(), "COD_EVENTO": None, "CODIGO": None, "numero_cliente": "1"},
            },
            {
                "geometry": {"coordinates": [lon, lat]},
                "properties": {**_props(), "COD_EVENTO": "EVT-OK", "numero_cliente": "2"},
            },
        ]
    }
    _preparar_main(monkeypatch, tmp_path, feed)

    mod.main()

    c = mod.conectar_db()
    filas = _fetchall(c, "SELECT cod_evento FROM eventos")
    c.close()
    assert filas == [("EVT-OK",)]  # el registro sin identificador se descarto, sin colisionar


def test_main_incluye_evento_justo_en_el_borde_comunal(monkeypatch, tmp_path, punto_borde):
    lon, lat = punto_borde
    feed = {
        "features": [
            {
                "geometry": {"coordinates": [lon, lat]},
                "properties": {**_props(), "COD_EVENTO": "EVT-BORDE", "numero_cliente": "1"},
            },
        ]
    }
    _preparar_main(monkeypatch, tmp_path, feed)

    mod.main()

    c = mod.conectar_db()
    filas = _fetchall(c, "SELECT cod_evento FROM eventos")
    c.close()
    assert filas == [("EVT-BORDE",)]  # un punto justo en el limite tambien debe quedar dentro


def test_main_agrega_tipo_cod_avisos_e_ids_aviso_de_varios_clientes(monkeypatch, tmp_path, punto_dentro):
    """Un mismo COD_EVENTO puede llegar en varias filas (una por cliente
    afectado); cod_avisos/ids_aviso deben juntar los valores de todas."""
    lon, lat = punto_dentro
    feed = {
        "features": [
            {
                "geometry": {"coordinates": [lon, lat]},
                "properties": {**_props(), "COD_EVENTO": "EVT-MULTI", "numero_cliente": "1",
                               "COD_AVISO": 111, "ID_AVISO": "ID-1"},
            },
            {
                "geometry": {"coordinates": [lon, lat]},
                "properties": {**_props(), "COD_EVENTO": "EVT-MULTI", "numero_cliente": "2",
                               "COD_AVISO": 222, "ID_AVISO": "ID-2"},
            },
        ]
    }
    _preparar_main(monkeypatch, tmp_path, feed)

    mod.main()

    c = mod.conectar_db()
    row = _fetchone(
        c, "SELECT tipo, clientes_afectados, cod_avisos, ids_aviso FROM eventos WHERE cod_evento='EVT-MULTI'"
    )
    c.close()
    assert row == ("AVISO", 2, "111,222", "ID-1,ID-2")


# ----------------------------------------------------------------------
# Replica en Supabase (best-effort, dual-write)
# ----------------------------------------------------------------------
# Se simula Supabase con una segunda base Postgres LOCAL (enel_las_condes_test_supabase),
# distinta de la de "produccion de test" (enel_las_condes_test), para poder
# verificar que la replica realmente escribe en una base separada, sin
# depender de red ni de las credenciales reales de Supabase.

TEST_SUPABASE_DB_NAME = "enel_las_condes_test_supabase"


def _preparar_supabase_falsa(monkeypatch):
    monkeypatch.setattr(mod, "SUPABASE_DB_HOST", mod.DB_HOST)
    monkeypatch.setattr(mod, "SUPABASE_DB_PORT", mod.DB_PORT)
    monkeypatch.setattr(mod, "SUPABASE_DB_NAME", TEST_SUPABASE_DB_NAME)
    monkeypatch.setattr(mod, "SUPABASE_DB_USER", mod.DB_USER)
    monkeypatch.setattr(mod, "SUPABASE_DB_PASSWORD", mod.DB_PASSWORD)

    c = mod.conectar_supabase()
    mod.init_db(c)
    _execute(c, "TRUNCATE eventos, historico_versiones, trafos_afectados, trafos_versiones, descargos_programados, descargos_versiones, estado_sistema, dim_h3")
    c.commit()
    c.close()


def test_main_replica_a_supabase_cuando_esta_configurado(monkeypatch, tmp_path, punto_dentro):
    lon, lat = punto_dentro
    feed = {
        "features": [{
            "geometry": {"coordinates": [lon, lat]},
            "properties": {**_props(), "COD_EVENTO": "EVT-SUPA", "numero_cliente": "1"},
        }]
    }
    _preparar_main(monkeypatch, tmp_path, feed)
    _preparar_supabase_falsa(monkeypatch)

    mod.main()

    c_local = mod.conectar_db()
    fila_local = _fetchone(c_local, "SELECT cod_evento FROM eventos WHERE cod_evento='EVT-SUPA'")
    c_local.close()
    assert fila_local == ("EVT-SUPA",)

    c_supa = mod.conectar_supabase()
    fila_supa = _fetchone(c_supa, "SELECT cod_evento FROM eventos WHERE cod_evento='EVT-SUPA'")
    c_supa.close()
    assert fila_supa == ("EVT-SUPA",)  # tambien llego a la "Supabase" (base separada)


def test_main_sin_supabase_configurado_no_intenta_replicar(monkeypatch, tmp_path, punto_dentro):
    lon, lat = punto_dentro
    feed = {
        "features": [{
            "geometry": {"coordinates": [lon, lat]},
            "properties": {**_props(), "COD_EVENTO": "EVT-SIN-SUPA", "numero_cliente": "1"},
        }]
    }
    _preparar_main(monkeypatch, tmp_path, feed)
    monkeypatch.setattr(mod, "SUPABASE_DB_HOST", "")  # default: sin configurar

    mod.main()  # no debe lanzar ni intentar conectar a Supabase

    c_local = mod.conectar_db()
    fila_local = _fetchone(c_local, "SELECT cod_evento FROM eventos WHERE cod_evento='EVT-SIN-SUPA'")
    c_local.close()
    assert fila_local == ("EVT-SIN-SUPA",)


def test_main_continua_si_supabase_no_responde(monkeypatch, tmp_path, punto_dentro):
    """Si Supabase esta inalcanzable (host/puerto invalido, red caida, etc.)
    la corrida local no debe verse afectada: la escritura local ya se hizo
    antes de intentar la replica."""
    lon, lat = punto_dentro
    feed = {
        "features": [{
            "geometry": {"coordinates": [lon, lat]},
            "properties": {**_props(), "COD_EVENTO": "EVT-SUPA-CAIDA", "numero_cliente": "1"},
        }]
    }
    _preparar_main(monkeypatch, tmp_path, feed)
    monkeypatch.setattr(mod, "SUPABASE_DB_HOST", "localhost")
    monkeypatch.setattr(mod, "SUPABASE_DB_PORT", "59999")  # nada escuchando ahi

    mod.main()  # no debe lanzar excepcion pese a que Supabase no responde

    c_local = mod.conectar_db()
    fila_local = _fetchone(c_local, "SELECT cod_evento FROM eventos WHERE cod_evento='EVT-SUPA-CAIDA'")
    c_local.close()
    assert fila_local == ("EVT-SUPA-CAIDA",)  # la corrida local se completo igual


# ----------------------------------------------------------------------
# Feed 2/3: filtro comunal para geometrias Polygon (trafosAfectados/descargos)
# ----------------------------------------------------------------------

def test_filtrar_por_comuna_poligonos_incluye_dentro_y_filtra_tipo(geometria_polygon_dentro, poligono):
    malla = mod.cargar_malla_h3()
    features = [
        {"properties": {"TIPO": "TRAFO", "numpos": "1"}, "geometry": geometria_polygon_dentro},
        {"properties": {"TIPO": "DESCARGO", "numpos": "2"}, "geometry": geometria_polygon_dentro},
    ]
    seleccionados = mod._filtrar_por_comuna_poligonos(features, "TRAFO", poligono, malla)
    assert len(seleccionados) == 1
    assert seleccionados[0][0]["properties"]["numpos"] == "1"


def test_filtrar_por_comuna_poligonos_sin_filtro_tipo_acepta_todos(geometria_polygon_dentro, poligono):
    malla = mod.cargar_malla_h3()
    features = [{"properties": {"TIPO": "DESCARGO", "numpos": "1"}, "geometry": geometria_polygon_dentro}]
    seleccionados = mod._filtrar_por_comuna_poligonos(features, None, poligono, malla)
    assert len(seleccionados) == 1


def test_filtrar_por_comuna_poligonos_descarta_fuera_de_la_comuna(geometria_polygon_fuera, poligono):
    malla = mod.cargar_malla_h3()
    features = [{"properties": {"TIPO": "TRAFO", "numpos": "1"}, "geometry": geometria_polygon_fuera}]
    assert mod._filtrar_por_comuna_poligonos(features, "TRAFO", poligono, malla) == []


def test_filtrar_por_comuna_poligonos_ignora_geometria_invalida(poligono):
    malla = mod.cargar_malla_h3()
    features = [{"properties": {"TIPO": "TRAFO", "numpos": "1"}, "geometry": None}]
    assert mod._filtrar_por_comuna_poligonos(features, "TRAFO", poligono, malla) == []


def test_filtrar_por_comuna_poligonos_calcula_h3(geometria_polygon_dentro, poligono):
    malla = mod.cargar_malla_h3()
    features = [{"properties": {"TIPO": "TRAFO", "numpos": "1"}, "geometry": geometria_polygon_dentro}]
    seleccionados = mod._filtrar_por_comuna_poligonos(features, "TRAFO", poligono, malla)
    _, h3_index, en_malla_ref, lon, lat = seleccionados[0]
    assert h3_index is not None
    assert en_malla_ref in (True, False)


# ----------------------------------------------------------------------
# RF-05: cruce de avisos (feed 1) con trafos/descargos via INCIDENCIA
# ----------------------------------------------------------------------

def test_consolidar_avisos_por_incidencia_agrupa_direcciones_y_clientes():
    seleccionados_avisos = [
        ({"properties": {"COD_EVENTO": "EVT-1", "DIRECCION": "Calle A", "numero_cliente": "1"}}, None, None, None, None),
        ({"properties": {"COD_EVENTO": "EVT-1", "DIRECCION": "Calle B", "numero_cliente": "2"}}, None, None, None, None),
        ({"properties": {"CODIGO": "EVT-2", "DIRECCION": "Calle C", "numero_cliente": "3"}}, None, None, None, None),
    ]
    consolidado = mod._consolidar_avisos_por_incidencia(seleccionados_avisos)
    assert consolidado["EVT-1"]["direcciones"] == {"Calle A", "Calle B"}
    assert consolidado["EVT-1"]["clientes"] == {"1", "2"}
    assert consolidado["EVT-2"]["direcciones"] == {"Calle C"}


def test_consolidar_avisos_por_incidencia_ignora_sin_cod_evento():
    seleccionados_avisos = [({"properties": {"DIRECCION": "Calle A"}}, None, None, None, None)]
    assert mod._consolidar_avisos_por_incidencia(seleccionados_avisos) == {}


def test_clientes_afectados_poligono_prefiere_clitotal():
    assert mod._clientes_afectados_poligono({"CLITOTAL": "390"}, {"1", "2"}) == 390


def test_clientes_afectados_poligono_usa_fallback_si_clitotal_vacio():
    assert mod._clientes_afectados_poligono({"CLITOTAL": ""}, {"1", "2", "3"}) == 3


def test_clientes_afectados_poligono_usa_fallback_si_clitotal_no_numerico():
    assert mod._clientes_afectados_poligono({"CLITOTAL": " "}, {"1"}) == 1


def test_preparar_filas_polygon_cruza_con_avisos_por_incidencia():
    seleccionados = [
        ({"properties": {"numpos": "100", "INCIDENCIA": "EVT-1", "CLITOTAL": ""}}, "h3a", True, -70.5, -33.4),
    ]
    avisos_por_incidencia = {"EVT-1": {"direcciones": {"Calle A"}, "clientes": {"1", "2"}}}
    filas = mod._preparar_filas_polygon(seleccionados, avisos_por_incidencia)
    assert len(filas) == 1
    numpos, props, h3_index, en_malla_ref, lon, lat, clientes_afectados, direcciones = filas[0]
    assert numpos == "100"
    assert direcciones == "Calle A"
    assert clientes_afectados == 2


def test_preparar_filas_polygon_descarta_sin_numpos():
    seleccionados = [({"properties": {"INCIDENCIA": "EVT-1"}}, "h3a", True, -70.5, -33.4)]
    assert mod._preparar_filas_polygon(seleccionados, {}) == []


def test_clasificar_descargo_futuro_en_curso_finalizado():
    ahora = datetime(2026, 7, 27, 15, 0, 0)
    assert mod._clasificar_descargo("27-07-2026 16:00", "27-07-2026 18:00", ahora) == "futuro"
    assert mod._clasificar_descargo("27-07-2026 14:00", "27-07-2026 18:00", ahora) == "en_curso"
    assert mod._clasificar_descargo("27-07-2026 10:00", "27-07-2026 14:00", ahora) == "finalizado"
    assert mod._clasificar_descargo(None, "27-07-2026 18:00", ahora) is None


# ----------------------------------------------------------------------
# Feed 2: tabla trafos_afectados
# ----------------------------------------------------------------------

def _props_trafo(**kwargs):
    base = {
        "TIPO": "TRAFO", "TENSION": "MT", "INCIDENCIA": "DF1", "id_alim": "100",
        "FECHA_INICIO": "27-07-2026 09:00", "ESTADOINC": "Activo",
        "FECHA_REPOSICION": "27-07-2026 13:00",
    }
    base.update(kwargs)
    return base


def test_upsert_trafo_inserta_nuevo(conn):
    mod.upsert_trafo(conn, "2026-07-27 09:00:00", "NP-1", _props_trafo(), "h3a", True, -70.5, -33.4, 5, "Calle A,Calle B")
    conn.commit()
    row = _fetchone(
        conn, "SELECT incidencia, estadoinc, clientes_afectados, direcciones, activo FROM trafos_afectados WHERE numpos='NP-1'"
    )
    assert row == ("DF1", "Activo", 5, "Calle A,Calle B", 1)
    hist = _fetchone(conn, "SELECT COUNT(*) FROM trafos_versiones WHERE numpos='NP-1'")[0]
    assert hist == 1


def test_upsert_trafo_actualiza_estado_y_clientes_sin_duplicar(conn):
    mod.upsert_trafo(conn, "t1", "NP-1", _props_trafo(ESTADOINC="Activo"), "h3a", True, -70.5, -33.4, 5, "Calle A")
    mod.upsert_trafo(conn, "t2", "NP-1", _props_trafo(ESTADOINC="Resuelto"), "h3a", True, -70.5, -33.4, 8, "Calle A,Calle B")
    conn.commit()
    row = _fetchone(conn, "SELECT estadoinc, clientes_afectados, direcciones FROM trafos_afectados WHERE numpos='NP-1'")
    assert row == ("Resuelto", 8, "Calle A,Calle B")
    assert _fetchone(conn, "SELECT COUNT(*) FROM trafos_afectados")[0] == 1
    assert _fetchone(conn, "SELECT COUNT(*) FROM trafos_versiones WHERE numpos='NP-1'")[0] == 2


def test_marcar_resueltos_generico_aplica_a_trafos(conn):
    mod.upsert_trafo(conn, "t1", "NP-1", _props_trafo(), "h3a", True, -70.5, -33.4, 1, "")
    mod.upsert_trafo(conn, "t1", "NP-2", _props_trafo(), "h3b", True, -70.5, -33.4, 1, "")
    conn.commit()
    n = mod._marcar_resueltos_generico(conn, "trafos_afectados", "numpos", {"NP-1"}, "t2")
    conn.commit()
    assert n == 1
    assert _fetchone(conn, "SELECT activo FROM trafos_afectados WHERE numpos='NP-2'")[0] == 0
    assert _fetchone(conn, "SELECT activo FROM trafos_afectados WHERE numpos='NP-1'")[0] == 1


def test_exportar_csv_trafos_escribe_solo_activos(conn, tmp_path, monkeypatch):
    path = tmp_path / "trafos.csv"
    monkeypatch.setattr(mod, "CSV_TRAFOS_ACTIVOS_PATH", path)
    mod.upsert_trafo(conn, "t1", "NP-1", _props_trafo(), "h3a", True, -70.5, -33.4, 1, "Calle A")
    mod.upsert_trafo(conn, "t1", "NP-2", _props_trafo(), "h3b", True, -70.5, -33.4, 1, "Calle B")
    conn.commit()
    mod._marcar_resueltos_generico(conn, "trafos_afectados", "numpos", {"NP-1"}, "t2")
    conn.commit()

    mod.exportar_csv_trafos(conn)

    txt = path.read_text(encoding="utf-8-sig")
    assert "NP-1" in txt
    assert "NP-2" not in txt


# ----------------------------------------------------------------------
# Feed 3: tabla descargos_programados
# ----------------------------------------------------------------------

def _props_descargo(**kwargs):
    base = {
        "TIPO": "DESCARGO", "TENSION": "MT", "INCIDENCIA": "DF2", "id_alim": "200",
        "DESCARGO": "DF2(TP1)", "FECHA_INIDESC": "27-07-2026 14:00", "FECHA_FINDESC": "27-07-2026 17:00",
        "CLI_PLAN": "1", "ESTADODESC": "789", "FECHA_REPOSICION": "27-07-2026 17:00",
    }
    base.update(kwargs)
    return base


def test_upsert_descargo_inserta_nuevo(conn):
    mod.upsert_descargo(conn, "2026-07-27 09:00:00", "ND-1", _props_descargo(), "h3a", True, -70.5, -33.4, 3, "Calle X")
    conn.commit()
    row = _fetchone(
        conn,
        "SELECT descargo_codigo, fecha_inidesc, fecha_findesc, clientes_afectados, activo "
        "FROM descargos_programados WHERE numpos='ND-1'",
    )
    assert row == ("DF2(TP1)", "27-07-2026 14:00", "27-07-2026 17:00", 3, 1)
    hist = _fetchone(conn, "SELECT COUNT(*) FROM descargos_versiones WHERE numpos='ND-1'")[0]
    assert hist == 1


def test_marcar_resueltos_generico_aplica_a_descargos(conn):
    mod.upsert_descargo(conn, "t1", "ND-1", _props_descargo(), "h3a", True, -70.5, -33.4, 1, "")
    conn.commit()
    n = mod._marcar_resueltos_generico(conn, "descargos_programados", "numpos", set(), "t2")
    conn.commit()
    assert n == 1
    assert _fetchone(conn, "SELECT activo FROM descargos_programados WHERE numpos='ND-1'")[0] == 0


def test_exportar_csv_descargos_incluye_todos_y_clasifica(conn, tmp_path, monkeypatch):
    path = tmp_path / "descargos.csv"
    monkeypatch.setattr(mod, "CSV_DESCARGOS_PATH", path)
    monkeypatch.setattr(mod, "datetime", _FakeDatetime)  # ahora fija: 2026-07-24 12:00:00

    mod.upsert_descargo(
        conn, "t1", "ND-1",
        _props_descargo(FECHA_INIDESC="24-07-2026 06:00", FECHA_FINDESC="24-07-2026 10:00"),
        "h3a", True, -70.5, -33.4, 1, "Calle A",
    )
    conn.commit()
    mod._marcar_resueltos_generico(conn, "descargos_programados", "numpos", set(), "t2")
    conn.commit()

    mod.exportar_csv_descargos(conn)

    filas = {r["NumPos"]: r for r in csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines(), delimiter=";")}
    assert filas["ND-1"]["EstadoTemporal"] == "finalizado"  # 06:00-10:00 ya paso respecto a las 12:00
    assert filas["ND-1"]["Activo"] == "0"  # el CSV de descargos incluye tambien los ya inactivos


# ----------------------------------------------------------------------
# Feed 4: tabla estado_sistema
# ----------------------------------------------------------------------

def test_insertar_estado_sistema(conn):
    mod.insertar_estado_sistema(conn, "2026-07-27 09:00:00", "27/07 09:00", 8)
    conn.commit()
    row = _fetchone(conn, "SELECT enel_datos, porcentaje FROM estado_sistema WHERE snapshot_ts='2026-07-27 09:00:00'")
    assert row == ("27/07 09:00", 8)


def test_insertar_estado_sistema_ignora_snapshot_duplicado(conn):
    mod.insertar_estado_sistema(conn, "t1", "27/07 09:00", 8)
    mod.insertar_estado_sistema(conn, "t1", "27/07 09:30", 9)  # mismo snapshot_ts que el anterior
    conn.commit()
    assert _fetchone(conn, "SELECT COUNT(*) FROM estado_sistema WHERE snapshot_ts='t1'")[0] == 1


# ----------------------------------------------------------------------
# main(): integracion de los 4 feeds (Fase 1 del documento de requerimientos)
# ----------------------------------------------------------------------

def test_main_integra_trafos_descargos_estado_con_cruce_de_avisos(
    monkeypatch, tmp_path, punto_dentro, geometria_polygon_dentro
):
    """Extremo a extremo: un aviso y un trafo comparten INCIDENCIA/COD_EVENTO;
    el trafo debe terminar con la direccion y los clientes del aviso (RF-05/06)."""
    lon, lat = punto_dentro
    feed_avisos = {
        "features": [{
            "geometry": {"coordinates": [lon, lat]},
            "properties": {**_props(), "COD_EVENTO": "DF-CRUCE", "DIRECCION": "Los Alamos 123", "numero_cliente": "999"},
        }]
    }
    feed_trafos = {
        "features": [{
            "properties": {
                "numpos": "NP-CRUCE", "TIPO": "TRAFO", "TENSION": "MT", "INCIDENCIA": "DF-CRUCE",
                "CLITOTAL": "", "id_alim": "500", "FECHA_INICIO": "27-07-2026 09:00",
                "ESTADOINC": "Activo", "FECHA_REPOSICION": "27-07-2026 13:00",
            },
            "geometry": geometria_polygon_dentro,
        }]
    }
    feed_descargos = {"features": []}
    feed_estado = {"datos": "27/07 09:45", "porcentaje": 8}

    _preparar_main(monkeypatch, tmp_path, feed_avisos)
    monkeypatch.setattr(mod, "descargar_trafos", lambda: feed_trafos)
    monkeypatch.setattr(mod, "descargar_descargos", lambda: feed_descargos)
    monkeypatch.setattr(mod, "descargar_estado", lambda: feed_estado)

    mod.main()

    c = mod.conectar_db()
    fila_trafo = _fetchone(
        c, "SELECT incidencia, direcciones, clientes_afectados FROM trafos_afectados WHERE numpos='NP-CRUCE'"
    )
    fila_estado = _fetchone(c, "SELECT enel_datos, porcentaje FROM estado_sistema ORDER BY snapshot_ts DESC LIMIT 1")
    c.close()
    assert fila_trafo == ("DF-CRUCE", "Los Alamos 123", 1)
    assert fila_estado == ("27/07 09:45", 8)


def test_main_trafos_falla_no_marca_previos_como_resueltos(monkeypatch, tmp_path, punto_dentro):
    """RNF-02: si el feed de trafos falla, no se toca la tabla esta
    corrida (a diferencia de una lista vacia legitima, que si resolveria)."""
    lon, lat = punto_dentro
    feed_avisos = {
        "features": [{
            "geometry": {"coordinates": [lon, lat]},
            "properties": {**_props(), "COD_EVENTO": "EVT-X", "numero_cliente": "1"},
        }]
    }
    _preparar_main(monkeypatch, tmp_path, feed_avisos)

    c = mod.conectar_db()
    mod.upsert_trafo(c, "t0", "NP-PREVIO", _props_trafo(), "h3a", True, -70.6, -33.4, 1, "")
    c.commit()
    c.close()

    def _falla_trafos():
        raise RuntimeError("timeout simulado")

    monkeypatch.setattr(mod, "descargar_trafos", _falla_trafos)
    monkeypatch.setattr(mod, "descargar_descargos", lambda: {"features": []})
    monkeypatch.setattr(mod, "descargar_estado", lambda: {"datos": "x", "porcentaje": 1})

    mod.main()

    c = mod.conectar_db()
    activo = _fetchone(c, "SELECT activo FROM trafos_afectados WHERE numpos='NP-PREVIO'")[0]
    c.close()
    assert activo == 1  # el feed de trafos fallo: no debe marcarse como resuelto


def test_main_trafos_vacio_top_level_no_marca_previos_como_resueltos(monkeypatch, tmp_path, punto_dentro):
    """Mismo caso anterior, pero disparado por un feed vacio (0 registros en
    todo Enel) en vez de una excepcion: tambien debe tratarse como fallo."""
    lon, lat = punto_dentro
    feed_avisos = {
        "features": [{
            "geometry": {"coordinates": [lon, lat]},
            "properties": {**_props(), "COD_EVENTO": "EVT-Y", "numero_cliente": "1"},
        }]
    }
    _preparar_main(monkeypatch, tmp_path, feed_avisos)

    c = mod.conectar_db()
    mod.upsert_trafo(c, "t0", "NP-PREVIO-2", _props_trafo(), "h3a", True, -70.6, -33.4, 1, "")
    c.commit()
    c.close()

    monkeypatch.setattr(mod, "descargar_trafos", lambda: {"features": []})
    monkeypatch.setattr(mod, "descargar_descargos", lambda: {"features": []})
    monkeypatch.setattr(mod, "descargar_estado", lambda: {"datos": "x", "porcentaje": 1})

    mod.main()

    c = mod.conectar_db()
    activo = _fetchone(c, "SELECT activo FROM trafos_afectados WHERE numpos='NP-PREVIO-2'")[0]
    c.close()
    assert activo == 1


def test_main_replica_trafos_descargos_estado_a_supabase(
    monkeypatch, tmp_path, punto_dentro, geometria_polygon_dentro
):
    lon, lat = punto_dentro
    feed_avisos = {
        "features": [{
            "geometry": {"coordinates": [lon, lat]},
            "properties": {**_props(), "COD_EVENTO": "EVT-SUPA-4F", "numero_cliente": "1"},
        }]
    }
    feed_trafos = {
        "features": [{
            "properties": {
                "numpos": "NP-SUPA", "TIPO": "TRAFO", "TENSION": "MT", "INCIDENCIA": "EVT-SUPA-4F",
                "CLITOTAL": "5", "id_alim": "500", "FECHA_INICIO": "27-07-2026 09:00",
                "ESTADOINC": "Activo", "FECHA_REPOSICION": "27-07-2026 13:00",
            },
            "geometry": geometria_polygon_dentro,
        }]
    }
    feed_descargos = {
        "features": [{
            "properties": {**_props_descargo(), "numpos": "ND-SUPA", "INCIDENCIA": "EVT-SUPA-4F-DESC"},
            "geometry": geometria_polygon_dentro,
        }]
    }
    feed_estado = {"datos": "27/07 10:00", "porcentaje": 5}

    _preparar_main(monkeypatch, tmp_path, feed_avisos)
    _preparar_supabase_falsa(monkeypatch)
    monkeypatch.setattr(mod, "descargar_trafos", lambda: feed_trafos)
    monkeypatch.setattr(mod, "descargar_descargos", lambda: feed_descargos)
    monkeypatch.setattr(mod, "descargar_estado", lambda: feed_estado)

    mod.main()

    c_supa = mod.conectar_supabase()
    trafo = _fetchone(c_supa, "SELECT numpos FROM trafos_afectados WHERE numpos='NP-SUPA'")
    descargo = _fetchone(c_supa, "SELECT numpos FROM descargos_programados WHERE numpos='ND-SUPA'")
    estado = _fetchone(c_supa, "SELECT enel_datos, porcentaje FROM estado_sistema WHERE enel_datos='27/07 10:00'")
    c_supa.close()
    assert trafo == ("NP-SUPA",)
    assert descargo == ("ND-SUPA",)
    assert estado == ("27/07 10:00", 5)


# ----------------------------------------------------------------------
# Fase 3: verificacion de integridad y purga del historico
# ----------------------------------------------------------------------

def test_verificar_integridad_db_sin_faltantes(conn):
    assert mod.verificar_integridad_db(conn) == []


def test_verificar_integridad_db_detecta_tabla_faltante(conn):
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE estado_sistema RENAME TO estado_sistema_temp")
    conn.commit()
    try:
        faltantes = mod.verificar_integridad_db(conn)
        assert "estado_sistema" in faltantes
    finally:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE estado_sistema_temp RENAME TO estado_sistema")
        conn.commit()


def test_purgar_historico_elimina_filas_viejas_pero_conserva_recientes(conn):
    vieja = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d %H:%M:%S")
    reciente = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")

    mod.upsert_evento(conn, vieja, "EVT-VIEJO", _props(), "h3a", True, -70.6, -33.4, 1, "1", "900001", "ID-AAA")
    mod.upsert_evento(conn, reciente, "EVT-RECIENTE", _props(), "h3b", True, -70.6, -33.4, 1, "2", "900002", "ID-BBB")
    conn.commit()

    eliminadas = mod.purgar_historico(conn, dias_retencion=90)

    assert eliminadas == 1
    assert _fetchone(conn, "SELECT COUNT(*) FROM historico_versiones WHERE cod_evento='EVT-VIEJO'")[0] == 0
    assert _fetchone(conn, "SELECT COUNT(*) FROM historico_versiones WHERE cod_evento='EVT-RECIENTE'")[0] == 1
    # las tablas resumen (estado actual) no se tocan aunque el evento sea viejo
    assert _fetchone(conn, "SELECT COUNT(*) FROM eventos WHERE cod_evento='EVT-VIEJO'")[0] == 1


def test_purgar_historico_purga_trafos_y_descargos_tambien(conn):
    vieja = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d %H:%M:%S")
    mod.upsert_trafo(conn, vieja, "NP-VIEJO", _props_trafo(), "h3a", True, -70.6, -33.4, 1, "")
    mod.upsert_descargo(conn, vieja, "ND-VIEJO", _props_descargo(), "h3a", True, -70.6, -33.4, 1, "")
    conn.commit()

    mod.purgar_historico(conn, dias_retencion=90)

    assert _fetchone(conn, "SELECT COUNT(*) FROM trafos_versiones WHERE numpos='NP-VIEJO'")[0] == 0
    assert _fetchone(conn, "SELECT COUNT(*) FROM descargos_versiones WHERE numpos='ND-VIEJO'")[0] == 0
    # las tablas resumen conservan su fila actual
    assert _fetchone(conn, "SELECT COUNT(*) FROM trafos_afectados WHERE numpos='NP-VIEJO'")[0] == 1
    assert _fetchone(conn, "SELECT COUNT(*) FROM descargos_programados WHERE numpos='ND-VIEJO'")[0] == 1


def test_purgar_historico_usa_retencion_por_defecto_configurable(conn, monkeypatch):
    monkeypatch.setattr(mod, "RETENCION_DIAS_HISTORICO", 1)
    vieja = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    mod.upsert_evento(conn, vieja, "EVT-VIEJO-2", _props(), "h3a", True, -70.6, -33.4, 1, "1", "900001", "ID-AAA")
    conn.commit()

    eliminadas = mod.purgar_historico(conn)  # sin argumento: usa RETENCION_DIAS_HISTORICO

    assert eliminadas == 1


def test_main_sale_con_codigo_de_error_si_falla_la_verificacion_de_integridad(monkeypatch):
    def _conectar_falla():
        raise RuntimeError("db caida")

    monkeypatch.setattr(mod, "conectar_db", _conectar_falla)

    with pytest.raises(SystemExit) as exc_info:
        mod.main()
    assert exc_info.value.code != 0


def test_main_purga_historico_como_parte_de_la_corrida(monkeypatch, tmp_path, punto_dentro):
    """Confirma que main() efectivamente invoca la purga (no solo que la
    funcion funcione aislada): una fila vieja preexistente debe desaparecer
    despues de una corrida normal."""
    lon, lat = punto_dentro
    feed = {
        "features": [{
            "geometry": {"coordinates": [lon, lat]},
            "properties": {**_props(), "COD_EVENTO": "EVT-CORRIDA", "numero_cliente": "1"},
        }]
    }
    _preparar_main(monkeypatch, tmp_path, feed)

    vieja = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
    c = mod.conectar_db()
    mod.upsert_evento(c, vieja, "EVT-YA-VIEJO", _props(), "h3a", True, -70.6, -33.4, 1, "1", "900001", "ID-AAA")
    c.commit()
    c.close()

    mod.main()

    c = mod.conectar_db()
    n = _fetchone(c, "SELECT COUNT(*) FROM historico_versiones WHERE cod_evento='EVT-YA-VIEJO'")[0]
    c.close()
    assert n == 0  # la purga corrio como parte de main()


# ----------------------------------------------------------------------
# Fase 4: modelo relacional (dim_h3 + vistas)
# ----------------------------------------------------------------------

def test_poblar_dim_h3_incluye_malla_y_h3_usados(conn):
    mod.upsert_evento(conn, "t1", "EVT-1", _props(), "h3-evento-test", True, -70.6, -33.4, 1, "1", "900001", "ID-AAA")
    conn.commit()

    mod.poblar_dim_h3(conn)

    malla = mod.cargar_malla_h3()
    algun_hex_malla = next(iter(malla))
    assert _fetchone(conn, "SELECT en_malla_referencia FROM dim_h3 WHERE h3_index=%s", (algun_hex_malla,))[0] is True

    # el h3_index del evento no esta en la malla oficial, pero igual debe quedar en la dimension
    fila = _fetchone(conn, "SELECT lat, lon, en_malla_referencia FROM dim_h3 WHERE h3_index='h3-evento-test'")
    assert fila is None  # "h3-evento-test" no es un h3_index real, cell_to_latlng falla y se omite


def test_poblar_dim_h3_calcula_centroide_real(conn, punto_dentro):
    lon, lat = punto_dentro
    h3_index = mod.h3_de_punto(lat, lon)
    mod.upsert_evento(conn, "t1", "EVT-1", _props(), h3_index, True, lon, lat, 1, "1", "900001", "ID-AAA")
    conn.commit()

    mod.poblar_dim_h3(conn)

    fila = _fetchone(conn, "SELECT lat, lon FROM dim_h3 WHERE h3_index=%s", (h3_index,))
    assert fila is not None
    assert fila[0] == pytest.approx(lat, abs=0.01)
    assert fila[1] == pytest.approx(lon, abs=0.01)


def test_crear_vistas_vw_trafos_con_aviso_cruza_por_incidencia(conn):
    mod.upsert_evento(conn, "t1", "EVT-CRUCE", _props(FALLA="Falla de prueba"), "h3a", True, -70.6, -33.4, 1, "1", "900001", "ID-AAA")
    mod.upsert_trafo(conn, "t1", "NP-1", _props_trafo(INCIDENCIA="EVT-CRUCE"), "h3a", True, -70.6, -33.4, 1, "Calle A")
    conn.commit()

    mod.crear_vistas(conn)

    fila = _fetchone(conn, "SELECT aviso_falla FROM vw_trafos_con_aviso WHERE numpos='NP-1'")
    assert fila == ("Falla de prueba",)


def test_crear_vistas_vw_trafos_con_aviso_sin_match_devuelve_null(conn):
    mod.upsert_trafo(conn, "t1", "NP-2", _props_trafo(INCIDENCIA="SIN-AVISO"), "h3a", True, -70.6, -33.4, 1, "")
    conn.commit()

    mod.crear_vistas(conn)

    fila = _fetchone(conn, "SELECT aviso_falla FROM vw_trafos_con_aviso WHERE numpos='NP-2'")
    assert fila == (None,)


def test_crear_vistas_vw_descargos_con_aviso(conn):
    mod.upsert_evento(conn, "t1", "EVT-D", _props(DESC_EVENTO="desc de prueba"), "h3a", True, -70.6, -33.4, 1, "1", "900001", "ID-AAA")
    mod.upsert_descargo(conn, "t1", "ND-1", _props_descargo(INCIDENCIA="EVT-D"), "h3a", True, -70.6, -33.4, 1, "Calle B")
    conn.commit()

    mod.crear_vistas(conn)

    fila = _fetchone(conn, "SELECT aviso_descripcion FROM vw_descargos_con_aviso WHERE numpos='ND-1'")
    assert fila == ("desc de prueba",)


def test_crear_vistas_vw_cortes_unificado_incluye_los_3_feeds(conn):
    mod.upsert_evento(conn, "t1", "EVT-U", _props(), "h3a", True, -70.6, -33.4, 1, "1", "900001", "ID-AAA")
    mod.upsert_trafo(conn, "t1", "NP-U", _props_trafo(), "h3a", True, -70.6, -33.4, 1, "")
    mod.upsert_descargo(conn, "t1", "ND-U", _props_descargo(), "h3a", True, -70.6, -33.4, 1, "")
    conn.commit()

    mod.crear_vistas(conn)

    filas = _fetchall(
        conn,
        "SELECT tipo_fuente, identificador FROM vw_cortes_unificado "
        "WHERE identificador IN ('EVT-U', 'NP-U', 'ND-U') ORDER BY tipo_fuente",
    )
    assert filas == [("AVISO", "EVT-U"), ("DESCARGO", "ND-U"), ("TRAFO", "NP-U")]


def test_crear_vistas_vw_duracion_cortes_calcula_horas(conn):
    mod.upsert_evento(
        conn, "2026-07-27 12:00:00", "EVT-DUR",
        _props(FECHA_INI="27-07-2026 08:00"), "h3a", True, -70.6, -33.4, 1, "1", "900001", "ID-AAA",
    )
    conn.commit()
    mod._marcar_resueltos_generico(conn, "eventos", "cod_evento", set(), "2026-07-27 12:00:00")
    conn.commit()

    mod.crear_vistas(conn)

    fila = _fetchone(conn, "SELECT horas_duracion FROM vw_duracion_cortes WHERE identificador='EVT-DUR'")
    assert fila == (4.0,)  # 08:00 -> 12:00


def test_crear_vistas_vw_duracion_cortes_ignora_activos(conn):
    mod.upsert_evento(conn, "t1", "EVT-ACTIVO-DUR", _props(), "h3a", True, -70.6, -33.4, 1, "1", "900001", "ID-AAA")
    conn.commit()

    mod.crear_vistas(conn)

    assert _fetchone(conn, "SELECT COUNT(*) FROM vw_duracion_cortes WHERE identificador='EVT-ACTIVO-DUR'")[0] == 0


def test_main_pobla_dim_h3_y_crea_vistas(monkeypatch, tmp_path, punto_dentro):
    """Confirma que main() efectivamente invoca dim_h3/vistas como parte
    normal de la corrida, no solo que las funciones aisladas funcionen."""
    lon, lat = punto_dentro
    feed = {
        "features": [{
            "geometry": {"coordinates": [lon, lat]},
            "properties": {**_props(), "COD_EVENTO": "EVT-MODELO", "numero_cliente": "1"},
        }]
    }
    _preparar_main(monkeypatch, tmp_path, feed)

    mod.main()

    c = mod.conectar_db()
    fila_unificada = _fetchone(c, "SELECT tipo_fuente FROM vw_cortes_unificado WHERE identificador='EVT-MODELO'")
    h3_index = _fetchone(c, "SELECT h3_index FROM eventos WHERE cod_evento='EVT-MODELO'")[0]
    en_dim_h3 = _fetchone(c, "SELECT COUNT(*) FROM dim_h3 WHERE h3_index=%s", (h3_index,))[0]
    c.close()
    assert fila_unificada == ("AVISO",)
    assert en_dim_h3 == 1
