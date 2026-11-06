@echo off
chcp 65001 >nul
rem Разрешает входящие подключения к порту сервера в брандмауэре Windows. Нужно запустить один раз.
set PORT=8765

net session >nul 2>&1
if errorlevel 1 (
    echo Нужны права администратора: щелкните файл правой кнопкой и выберите "Запуск от имени администратора".
    echo.
    pause
    exit /b 1
)

netsh advfirewall firewall delete rule name="Secure AI Document Gateway" >nul 2>&1
netsh advfirewall firewall add rule name="Secure AI Document Gateway" dir=in action=allow protocol=TCP localport=%PORT%
if errorlevel 1 (
    echo Не удалось добавить правило брандмауэра.
) else (
    echo Готово: порт %PORT% открыт для входящих подключений.
)
echo.
pause