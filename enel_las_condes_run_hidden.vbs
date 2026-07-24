' Lanza enel_las_condes_run.bat sin mostrar ninguna ventana de consola.
' Usado por la tarea programada "Reporte_Enel" en el Programador de tareas
' de Windows (no requiere guardar contrasena: corre en la sesion del
' usuario que tenga la sesion iniciada).

Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
batPath = scriptDir & "\enel_las_condes_run.bat"

Set shell = CreateObject("WScript.Shell")
shell.Run """" & batPath & """", 0, True
