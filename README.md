# Reporte_Enel — CMU, Municipalidad de Las Condes

Pipeline que descarga el feed público de emergencias de Enel
(`mapaemergencia.enel.com`), lo filtra al límite comunal oficial de Las
Condes, lo enriquece con malla H3 y con las 25 unidades vecinales
(barrios) de la comuna, y mantiene un repositorio histórico para
alimentar Power BI, una API propia y Superset.

## Arquitectura

```
                    ┌──────────────────────────┐
Enel (4 feeds) ───► │ enel_las_condes_historico │ ── CSV ──► Power BI
                    │           .py             │
                    └────────────┬──────────────┘
                                 │
                    ┌────────────┴──────────────┐
                    │                            │
              Postgres local              Supabase (réplica)
                    │                            │
              enel_las_condes_api.py ──────► Superset (Docker)
```

- **`enel_las_condes_historico.py`** — se ejecuta cada 30 min vía Task
  Scheduler (tarea `Reporte_Enel`). Descarga los 4 feeds, filtra, cruza,
  escribe en Postgres local (obligatorio) y replica a Supabase
  (best-effort — si Supabase falla no afecta la corrida local).
- **`enel_las_condes_api.py`** — API FastAPI de solo lectura sobre
  cualquiera de las dos bases, para Power BI/Superset/lo que sea sin
  depender de los CSV.
- **Superset** (Docker, `docker-compose-non-dev.yml` del repo oficial de
  Apache Superset) — conectado tanto al Postgres local
  (`host.docker.internal`) como a Supabase.
- **Tests** — corren contra una base Postgres de prueba real
  (`enel_las_condes_test`), nunca contra producción.

Credenciales de conexión via variables de entorno (ver
`enel_las_condes_secrets.example.bat`): `ENEL_DB_*` (Postgres local) y
`SUPABASE_DB_*` (réplica, opcional — si `SUPABASE_DB_HOST` queda vacío
se omite sin afectar la corrida).

## Los 5 feeds de Enel integrados

| # | Endpoint | Contenido | Geometría | Tabla Postgres |
|---|---|---|---|---|
| 1 | `me-capa-avisos.txt` | Avisos individuales de clientes por falla no planificada | Point | `eventos` + `historico_versiones` |
| 2 | `me-capa-trafosAfectados.txt` | Transformadores afectados. **El feed mezcla dos tipos**: `TIPO=TRAFO` (incidentes reales) y `TIPO=DESCARGO` (cortes programados) — solo se usa la parte `TRAFO` de este feed | Polygon | `trafos_afectados` + `trafos_versiones` |
| 3 | `me-capa-descargos.txt` | Cortes programados/mantenimiento (feed dedicado, mismo esquema que las filas `TIPO=DESCARGO` de trafosAfectados) | Polygon | `descargos_programados` + `descargos_versiones` |
| 4 | `me-capa-estado.txt` | Health-check general del sistema Enel (`{errorCode, msg, datos, porcentaje}`) | N/A | `estado_sistema` |
| 5 | `me-capa-comunasAfectadas.txt` | Resumen por comuna de toda la concesión (una feature por comuna). Es el origen del texto que muestra el mapa de Enel, p. ej. `LAS_CONDES: 16 clientes afectados (0%)`. Solo se guarda la fila de Las Condes | Polygon (no se usa) | `comuna_estado` |

**Cruce (RF-05):** los feeds 2 y 3 traen `INCIDENCIA`, que coincide en
formato con `COD_EVENTO`/`CODIGO` del feed 1 (ej. `DF202671756176`). El
script cruza ambos para completar `Direcciones` y `ClientesAfectados`
(prefiriendo `CLITOTAL` oficial de Enel cuando viene informado; si no,
cuenta los `numero_cliente` distintos de los avisos cruzados).

**Filtro comunal:** point-in-polygon inclusivo contra
`Limite_Comunal_LasCondes.geojson` (`Polygon.covers`: entra un punto que
cae dentro o que toca exactamente el borde). Para los feeds 2 y 3
(geometría Polygon) se usa el `representative_point()` de cada polígono,
que a diferencia del centroide queda garantizado dentro de la forma.

**Resiliencia (RNF-02):** cada feed se descarga y procesa de forma
independiente. Si uno falla (timeout, JSON corrupto, o llega vacío a
nivel global — señal de falla transitoria de la API) esa corrida **no
marca nada de ese feed como resuelto**: se preserva el estado anterior
hasta la próxima corrida exitosa. Esto es distinto de un feed que
legítimamente devuelve 0 registros para Las Condes (que sí resuelve lo
que corresponda).

### Otras capas del mapa de Enel (detectadas, sin integrar)

Revisando el código fuente de `mapaemergencia.enel.com` (`js/featuresVisibilityObject.js`
y `mapa.js`) aparecen banderas de visibilidad para capas adicionales a
las integradas. La capa `comuna` resultó ser el feed 5
(`me-capa-comunasAfectadas.txt`, ya integrado: trae `CLIENTESTOTAL`,
`CLIENTESAFECTADOS` y `PORCENTAJE` por comuna, además del polígono —
seguimos usando nuestro propio límite oficial municipal para filtrar).
Siguen sin integrar: `electro`, `libres`, `alim` (alimentadores), `cuadrillas`
(equipos en terreno), `smt`/`smtTns` y `clinica`. También existe una
empresa "Colina" (`empresa=C`) con su propio set paralelo de capas
(avisos/descargos/comuna/alimentador), que corresponde a una zona/
distribuidora distinta a Las Condes.

**Ninguna de estas tiene un endpoint público confirmado** (`mapa.js`
nombra `me-capa-alimAfectados.txt`, `me-capa-electro.txt`,
`me-capa-libres.txt` y `me-capa-smt.txt`, pero en la ruta
`galeria/documento` devuelven 404) — se probaron
variantes de nombre razonables (`me-capa-electro.txt`, `me-capa-libres.txt`,
`me-capa-comunas.txt`, `me-capa-alimentadores.txt`, `me-capa-smt.txt`,
`me-capa-cuadrillas.txt`, `me-capa-clinica.txt`, etc.) y todas devuelven
404. Quedan registradas aquí para que quien retome esto no repita la
búsqueda desde cero; si en el futuro se identifica el endpoint real,
lo natural es replicar el mismo patrón que `trafosAfectados`/`descargos`.

## Diccionario de datos (CSV)

Todos los CSV usan delimitador `;`, encoding UTF-8 con BOM (`utf-8-sig`,
para que Excel/Power BI los abra bien), y se regeneran completos en
cada corrida (no se anexan filas).

### `enel_las_condes_eventos_activos.csv` (feed 1, solo activos)

| Columna | Origen | Descripción |
|---|---|---|
| CodigoEvento | `COD_EVENTO`/`CODIGO` | Identificador único del evento (clave primaria) |
| Codigo | `CODIGO` | Código crudo de Enel |
| Tipo | `TIPO` | `AVISO` / `AVISOC` |
| Direccion | `DIRECCION` | Dirección del primer aviso visto para este evento |
| ClientesAfectados | calculado | Clientes distintos (`numero_cliente`) reportados para el evento |
| FechaInicio | `FECHA_INI` | Hora de inicio del corte (`DD-MM-YYYY HH:MM`, texto de Enel) |
| FechaReposicionEstimada | `FECHA_REPOSICION` | Hora estimada de reposición, se actualiza cada corrida |
| DetalleFalla | `FALLA` | Detalle de la falla, se actualiza cada corrida |
| DescripcionEvento | `DESC_EVENTO` | Descripción libre de Enel |
| Alimentador | `id_alim` | Alimentador eléctrico |
| H3Index | calculado | Hexágono H3 resolución 8 del punto |
| EnMallaH3Referencia | calculado | 1 si el hexágono está en `H3_LasCondes_Res8.geojson` |
| UnidadVecinal | calculado | Código de la unidad vecinal (`C-1`..`C-25`) que contiene el punto, según `Unidades_Vecinales_LasCondes.geojson`; `null` si no cae dentro de ninguna |
| Latitud / Longitud | geometría | Coordenadas del punto |
| ClientesUnicos | calculado | Lista de `numero_cliente` distintos, separados por coma |
| CodigosAviso | calculado | Lista de `COD_AVISO` distintos del evento |
| IdsAviso | calculado | Lista de `ID_AVISO` distintos del evento |
| PrimeraVezVisto / UltimaVezVisto | snapshot | Timestamps de nuestra propia corrida (`YYYY-MM-DD HH:MM:SS`) |
| HorasActivo | calculado | Horas desde `FechaInicio` hasta el momento del reporte |

### `enel_las_condes_eventos_historico.csv` (feed 1, todos)

Mismas columnas que el anterior, más:

| Columna | Descripción |
|---|---|
| Activo | 1 = activo, 0 = resuelto |
| FechaResolucionDetectada | Snapshot en que se detectó que el evento dejó de aparecer en el feed |
| HorasActivo | Para resueltos, se congela en `FechaResolucionDetectada` (no sigue creciendo) |

### `enel_las_condes_trafos_activos.csv` (feed 2, solo `TIPO=TRAFO` activos)

| Columna | Origen | Descripción |
|---|---|---|
| NumPos | `numpos` | Identificador de Enel del registro (clave primaria) |
| Incidencia | `INCIDENCIA` | Cruza con `CodigoEvento` del feed de avisos |
| Tipo | `TIPO` | Siempre `TRAFO` en este CSV |
| Tension | `TENSION` | Nivel de tensión (ej. `MT`) |
| Alimentador | `id_alim` | Alimentador eléctrico |
| H3Index / EnMallaH3Referencia | calculado | Igual que en avisos, sobre el `representative_point()` del polígono |
| UnidadVecinal | calculado | Igual que en avisos, sobre el `representative_point()` del polígono |
| Latitud / Longitud | geometría | Coordenadas del `representative_point()` |
| ClientesAfectados | `CLITOTAL` o fallback | Oficial de Enel si viene informado; si no, clientes distintos cruzados desde avisos |
| Direcciones | cruce RF-05 | Direcciones de los avisos con la misma `INCIDENCIA`, separadas por coma |
| EstadoIncidencia | `ESTADOINC` | Estado textual de Enel (ej. `Activo`) |
| FechaInicio | `FECHA_INICIO` | |
| FechaReposicionEstimada | `FECHA_REPOSICION` | Se actualiza cada corrida |
| PrimeraVezVisto / UltimaVezVisto | snapshot | Igual que en avisos |

### `enel_las_condes_trafos_historico.csv` (feed 2, todos)

Mismas columnas que el anterior, más:

| Columna | Descripción |
|---|---|
| Activo | 1 = activo, 0 = resuelto |
| FechaResolucionDetectada | Snapshot en que se detectó que el `numpos` dejó de aparecer en el feed |
| HorasActivo | Para resueltos, se congela en `FechaResolucionDetectada` (no sigue creciendo) |

### `enel_las_condes_descargos.csv` (feed 3, **todos**, no solo activos)

| Columna | Origen | Descripción |
|---|---|---|
| NumPos | `numpos` | Clave primaria |
| Incidencia | `INCIDENCIA` | Cruza con `CodigoEvento` del feed de avisos |
| DescargoCodigo | `DESCARGO` | Código propio del descargo (ej. `DF...( TP...)`) |
| Tipo / Tension / Alimentador | igual que trafos | |
| H3Index / EnMallaH3Referencia / UnidadVecinal / Latitud / Longitud | calculado | |
| ClientesAfectados | `CLITOTAL` o fallback | |
| Direcciones | cruce RF-05 | |
| EstadoDescargo | `ESTADODESC` | |
| FechaInicioDescargo / FechaFinDescargo | `FECHA_INIDESC` / `FECHA_FINDESC` | Ventana programada del corte |
| FechaReposicionEstimada | `FECHA_REPOSICION` | |
| PrimeraVezVisto / UltimaVezVisto | snapshot | |
| Activo | calculado | 1 mientras el `numpos` siga apareciendo en el feed |
| EstadoTemporal | calculado (RF-07) | `futuro` / `en_curso` / `finalizado`, comparando la ventana horaria contra el momento del reporte |

### `enel_las_condes_comuna_estado.csv` (feed 5, una fila por corrida)

Historial del resumen de Las Condes según Enel, más reciente primero.

| Columna | Origen | Descripción |
|---|---|---|
| SnapshotTs | snapshot | Timestamp de nuestra corrida (`YYYY-MM-DD HH:MM:SS`) |
| Comuna / CodVnr / Centro | `COMUNA` / `COD_VNR` / `CENTRO` | Identificación de la comuna en Enel (`LAS_CONDES`, `13114`, `CENTRO_ORIENTE`) |
| ClientesTotal | `CLIENTESTOTAL` | Clientes totales de la comuna |
| ClientesAfectados | `CLIENTESAFECTADOS` | Clientes afectados según Enel (el número del mapa) |
| PorcentajeEnel | `PORCENTAJE` | Porcentaje tal como lo informa Enel (1 decimal, por eso suele verse `0`) |
| PorcentajeCalculado | calculado | `ClientesAfectados / ClientesTotal * 100` con 4 decimales |

Este número es el oficial de Enel para la comuna y **no necesariamente
coincide** con la suma de `ClientesAfectados` de nuestros avisos/trafos
(son fuentes distintas).

### `enel_las_condes_descargos_activos.csv` (feed 3, solo activos)

Mismas columnas que `enel_las_condes_descargos.csv`, salvo `Activo` (se
omite, ya que siempre sería 1 al estar filtrado).

## Modelo relacional para visualización (Fase 4)

Los 3 feeds geográficos (avisos, trafos, descargos) comparten tres claves
naturales — `INCIDENCIA`/`COD_EVENTO`, `h3_index` y `unidad_vecinal` —
pero viven en tablas separadas. `crear_vistas()`, `poblar_dim_h3()` y
`poblar_dim_unidad_vecinal()` (llamadas cada corrida, en Postgres local y
en la réplica de Supabase) arman el modelo relacional sobre esas claves,
para que Power BI o Superset lo consuman directo sin reimplementar los
joins:

| Objeto | Tipo | Contenido |
|---|---|---|
| `dim_h3` | Tabla (dimensión) | Un registro por hexágono H3 usado en cualquiera de los 3 feeds o en la malla de referencia, con su centroide (`lat`/`lon` via `h3.cell_to_latlng`) y si pertenece a la malla oficial |
| `dim_unidad_vecinal` | Tabla (dimensión) | Un registro por cada una de las 25 unidades vecinales (`codigo` `C-1`..`C-25`, `numero`), con un punto de referencia garantizado dentro del polígono (`representative_point()`) y su geometría completa como GeoJSON (`geometria_geojson`), para mapear por polígono |
| `vw_trafos_con_aviso` | Vista | `trafos_afectados` + los campos del aviso de origen (`falla`, `desc_evento`, `fecha_ini`) cruzados por `INCIDENCIA = cod_evento` |
| `vw_descargos_con_aviso` | Vista | Mismo cruce, para `descargos_programados` |
| `vw_cortes_unificado` | Vista (UNION ALL) | Los 3 feeds normalizados a un solo "hecho": `tipo_fuente` (`AVISO`/`TRAFO`/`DESCARGO`), `identificador`, `incidencia`, `h3_index`, `lat`/`lon`, `clientes_afectados`, `direcciones`, `fecha_inicio`, `activo`, `unidad_vecinal` — la fuente para mapas y series de tiempo cruzados entre feeds |
| `vw_duracion_cortes` | Vista | Horas de duración (`fecha_ini` → `fecha_resolucion_detectada`) de cada aviso ya resuelto, por `alimentador` — base del ranking de duración |
| `vw_mapa_h3` | Vista | `vw_cortes_unificado` (solo activos) agregado por `h3_index` vía `dim_h3`: total de clientes afectados y conteo por tipo de fuente, por hexágono — un registro por hexágono, listo para mapear |
| `vw_mapa_unidad_vecinal` | Vista | `vw_cortes_unificado` (solo activos) agregado por `unidad_vecinal` vía `dim_unidad_vecinal`: total de clientes afectados y conteo por tipo de fuente, por barrio — un registro por unidad vecinal, listo para mapear |

**Unidades vecinales (barrio):** `Unidades_Vecinales_LasCondes.geojson`
trae los 25 polígonos oficiales (exportados desde KML; el código `C-<n>`
viene embebido como texto en `properties.descriptio`).
`cargar_unidades_vecinales()` los parsea una vez por corrida y
`unidad_vecinal_de_punto()` clasifica cada punto (mismo criterio
inclusivo `covers()` que el filtro comunal) — igual patrón que la malla
H3, pero pensado para reportar/filtrar por barrio en vez de por hexágono.

**Mapa geográfico:** este Superset (Docker) sí trae los plugins de
mapas (deck.gl + MapLibre/Carto, sin necesitar token de Mapbox — el
renderer por defecto usa mosaicos abiertos de Carto/OpenStreetMap). Los
charts de mapa usan el tipo **deck.gl Scatterplot** sobre `vw_mapa_h3` y
`vw_mapa_unidad_vecinal`: un punto por hexágono/barrio, con tamaño
proporcional a `clientes_afectados_total`.

### Dashboard en Superset

`Reporte Enel Las Condes - Modelo Relacional` (`/superset/dashboard/reporte-enel-modelo-relacional/`):
- **Serie de tiempo — clientes afectados**: `vw_cortes_unificado`, `SUM(clientes_afectados)` por día, desglosado por `tipo_fuente`.
- **Ranking de duración de cortes por alimentador**: `vw_duracion_cortes`, promedio de `horas_duracion` por `alimentador`, descendente.
- **Mapa por H3Index — clientes afectados por hexágono**: `vw_mapa_h3`, deck.gl Scatterplot sobre el centroide de cada hexágono, tamaño = clientes afectados.
- **Mapa por Unidad Vecinal — clientes afectados**: `vw_mapa_unidad_vecinal`, deck.gl Scatterplot sobre el punto de referencia de cada barrio, tamaño = clientes afectados.

Datasets registrados en Superset (conexión "Postgres Local - Enel Las
Condes"): `vw_cortes_unificado`, `vw_trafos_con_aviso`,
`vw_descargos_con_aviso`, `vw_duracion_cortes`, `vw_mapa_h3`, `dim_h3`,
`vw_mapa_unidad_vecinal`, `dim_unidad_vecinal`.

## API (FastAPI)

`uvicorn enel_las_condes_api:app --host 0.0.0.0 --port 8000` — ver
`/docs` para la documentación interactiva. Mismos campos que los CSV
correspondientes, vía `/eventos/activos`, `/eventos/historico`,
`/eventos/{cod_evento}/versiones`, `/trafos/activos`, `/descargos`,
`/estado`, `/comuna/estado`, `/health`, `/unidades-vecinales` (dimensión de las 25
unidades vecinales, con su geometría).

## Mantenimiento del histórico (Fase 3)

- **Verificación de integridad**: al inicio de cada corrida, antes de
  gastar tiempo descargando los 4 feeds, se conecta a Postgres, se
  asegura el esquema (`init_db`, crea tablas/columnas si faltan) y se
  registra en el log si falta alguna tabla esperada. Si la conexión
  falla, la corrida se aborta (`exit code` distinto de cero) sin llegar
  a tocar Enel.
- **Purga de snapshots crudos** (`historico_versiones`, `trafos_versiones`,
  `descargos_versiones`): se ejecuta al final de cada corrida (local y
  réplica), borrando filas más viejas que `ENEL_RETENCION_DIAS_HISTORICO`
  (variable de entorno, default 90 días). **Las tablas de resumen**
  (`eventos`, `trafos_afectados`, `descargos_programados`) **nunca se
  purgan** — conservan el estado/resumen actual de cada evento
  independiente de su antigüedad.

## Correr los tests

```bash
ENEL_DB_PASSWORD=... python -m pytest test_enel_las_condes_historico.py test_enel_las_condes_api.py -v
```

Requiere una base `enel_las_condes_test` en el mismo servidor Postgres
(se crea una sola vez con `createdb enel_las_condes_test`); los tests
la truncan antes de cada caso, nunca tocan `enel_las_condes` (producción).
