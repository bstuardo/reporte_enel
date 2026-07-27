rem Copia este archivo como "enel_las_condes_secrets.bat" (sin el .example)
rem y completa tus credenciales reales. enel_las_condes_secrets.bat esta en
rem .gitignore: nunca se sube al repositorio.

set "ENEL_DB_HOST=localhost"
set "ENEL_DB_PORT=5432"
set "ENEL_DB_NAME=enel_las_condes"
set "ENEL_DB_USER=postgres"
set "ENEL_DB_PASSWORD=cambiar_esto"

rem Replica opcional en Supabase (Session pooler). Dejar SUPABASE_DB_HOST
rem vacio si no se quiere replicar - el script lo omite automaticamente.
set "SUPABASE_DB_HOST="
set "SUPABASE_DB_PORT=5432"
set "SUPABASE_DB_NAME=postgres"
set "SUPABASE_DB_USER="
set "SUPABASE_DB_PASSWORD="
