@echo off
pushd "%~dp0"
echo.
echo  CLAIR - Atelier de verification
echo  Ouvrez http://127.0.0.1:8765 dans votre navigateur.
echo  Gardez cette fenetre ouverte pendant le traitement.
echo.
py -3 server.py
popd
pause
