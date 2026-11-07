@echo off
REM Build a single-folder Windows app. Run from this folder inside your virtual environment.
REM NOT yet tested on Windows - see README "Known limitations".
pip install -r requirements.txt || exit /b 1
REM the database lives next to the .exe: keep it safe while dist\ is rebuilt
if exist dist\FactoryManagement\data (
  if exist _data_keep rmdir /s /q _data_keep
  mkdir _data_keep
  for %%D in (data backups recovery logs exports) do if exist dist\FactoryManagement\%%D xcopy /e /i /q dist\FactoryManagement\%%D _data_keep\%%D >nul
  if exist dist\FactoryManagement\ui.json copy /y dist\FactoryManagement\ui.json _data_keep\ui.json >nul
)
pyinstaller --noconfirm --windowed --name FactoryManagement ^
  --add-data "database\migrations;database\migrations" ^
  --collect-all customtkinter ^
  main.py
if exist _data_keep (
  xcopy /e /i /q /y _data_keep dist\FactoryManagement >nul
  echo Your data folder was restored. A safety copy is in _data_keep - delete it once you have checked the app.
)
echo.
echo Output: dist\FactoryManagement\FactoryManagement.exe
echo Data is stored next to the program: dist\FactoryManagement\data\factory.db
