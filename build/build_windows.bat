@echo off
rem 构建 dist\CodeFuel\CodeFuel.exe；分发时保留整个 CodeFuel 文件夹
setlocal
cd /d "%~dp0.."
if "%UV_DEFAULT_INDEX%"=="" set UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/
uv sync --locked --dev || goto :error
uv run --locked python build\make_icon.py || goto :error
set PYINSTALLER_CONFIG_DIR=%CD%\build\.pyinstaller
uv run --locked pyinstaller --noconfirm --distpath dist --workpath build\pyinstaller-codefuel build\CodeFuel.spec || goto :error
echo.
echo Done: %CD%\dist\CodeFuel\CodeFuel.exe
exit /b 0
:error
echo Build failed.
exit /b 1
