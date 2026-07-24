"""
Migracion unica: copia el historico de enel_las_condes.db (SQLite) a la
base PostgreSQL configurada en enel_las_condes_historico.py (variables
ENEL_DB_*). Pensado para correrse UNA vez al pasar de SQLite a Postgres.

Uso:
    python migrar_a_postgres.py
"""

import sqlite3
from pathlib import Path

import enel_las_condes_historico as mod

SQLITE_DB_PATH = Path(__file__).resolve().parent / "enel_las_condes.db"


def _migrar_tabla(sqlite_conn, pg_conn, tabla):
    cur_sqlite = sqlite_conn.execute(f"SELECT * FROM {tabla}")
    columnas = [d[0] for d in cur_sqlite.description]
    filas = cur_sqlite.fetchall()

    if not filas:
        print(f"  {tabla}: 0 filas en SQLite, nada que migrar")
        return 0

    placeholders = ",".join(["%s"] * len(columnas))
    columnas_sql = ",".join(columnas)
    insert_sql = f"INSERT INTO {tabla} ({columnas_sql}) VALUES ({placeholders})"
    if tabla == "eventos":
        insert_sql += " ON CONFLICT (cod_evento) DO NOTHING"

    with pg_conn.cursor() as cur:
        for fila in filas:
            cur.execute(insert_sql, tuple(fila))
    pg_conn.commit()
    print(f"  {tabla}: {len(filas)} filas migradas")
    return len(filas)


def main():
    if not SQLITE_DB_PATH.exists():
        print(f"No se encontro {SQLITE_DB_PATH}, nada que migrar.")
        return

    sqlite_conn = sqlite3.connect(str(SQLITE_DB_PATH))
    pg_conn = mod.conectar_db()
    try:
        mod.init_db(pg_conn)

        with pg_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM eventos")
            ya_existentes = cur.fetchone()[0]
        if ya_existentes:
            print(
                f"La tabla 'eventos' en Postgres ya tiene {ya_existentes} filas. "
                "Se omiten los duplicados por cod_evento (ON CONFLICT DO NOTHING), "
                "pero revisa si esto es lo esperado."
            )

        print("Migrando SQLite -> PostgreSQL:")
        _migrar_tabla(sqlite_conn, pg_conn, "eventos")
        _migrar_tabla(sqlite_conn, pg_conn, "historico_versiones")

        with pg_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM eventos")
            n_eventos = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM historico_versiones")
            n_hist = cur.fetchone()[0]
        print(f"Postgres ahora tiene: {n_eventos} eventos, {n_hist} snapshots historicos.")
    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    main()
