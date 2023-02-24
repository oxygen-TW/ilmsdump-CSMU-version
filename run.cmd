%echo off
chcp 65001
where python > nul 2>&1 || (echo Python is not installed. Please install python from https://www.python.org/downloads/ && exit /b)

echo Python is installed!

echo 正在安裝相依性套件
pip install -e .[dev]

echo 請輸入 iLMS 帳號與密碼
ilmsdump --login
echo 正在嘗試下載所有課程檔案
ilmsdump enrolled --dry
pause