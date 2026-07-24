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
from datetime import datetime

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
    _execute(c, "TRUNCATE eventos, historico_versiones")
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
            cur.execute("DROP TABLE IF EXISTS eventos, historico_versiones")
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
    _execute(c, "TRUNCATE eventos, historico_versiones")
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
