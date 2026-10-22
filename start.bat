@echo off
rem Double-click this file to start HomeStream.
rem It opens the project folder, turns on the virtual environment and runs start.py.
rem Add options after the name when running it from a terminal, for example: start.bat --local-only

cd /d "%~dp0"
call venv\Scripts\activate.bat
python start.py %*

rem Keep the window open if something went wrong, so you can read the message
pause
