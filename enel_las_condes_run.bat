@echo off
setlocal

set "PYTHON_EXE=C:\Python_3.14\python.exe"
set "SCRIPT_DIR=%~dp0"
set "SCRIPT=%SCRIPT_DIR%enel_las_condes_historico.py"
set "ERROR_LOG=%SCRIPT_DIR%enel_las_condes_bat_error.log"
set "TASK_LOG=%SCRIPT_DIR%enel_las_condes_task_log.txt"
set "SECRETS=%SCRIPT_DIR%enel_las_condes_secrets.bat"

rem Credenciales de conexion (host/usuario/password): ver enel_las_condes_secrets.example.bat
if exist "%SECRETS%" (
    call "%SECRETS%"
) else (
    echo %date% %time% ERROR falta %SECRETS% - copia enel_las_condes_secrets.example.bat >> "%TASK_LOG%"
    exit /b 1
)

cd /d "%SCRIPT_DIR%"

"%PYTHON_EXE%" "%SCRIPT%" >> "%ERROR_LOG%" 2>&1

if %ERRORLEVEL% EQU 0 (
    echo %date% %time% OK >> "%TASK_LOG%"
) else (
    echo %date% %time% ERROR codigo=%ERRORLEVEL% >> "%TASK_LOG%"
)

endlocal
