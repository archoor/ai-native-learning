# Create or update desktop shortcut for AiNativeLearning portable exe
# Use Unicode code points for Chinese label to avoid PS script encoding issues (UTF-8 vs GBK).
$DesktopDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $DesktopDir "..\..")).Path
# 使用 win-unpacked 目录版（含 resources/backend）；单文件 portable 不含 4GB+ 后端会启动失败
$ExePath = (Resolve-Path (Join-Path $DesktopDir "release\win-unpacked\AiNativeLearning.exe")).Path
$WorkDir = Split-Path $ExePath -Parent
$IconPath = (Resolve-Path (Join-Path $DesktopDir "app-icon.ico")).Path
$Desktop = [Environment]::GetFolderPath("Desktop")

# AI原生学习 (U+539F U+751F U+5B66 U+4E60)
$AppLabel = "AI" + [char]0x539F + [char]0x751F + [char]0x5B66 + [char]0x4E60
$ShortcutPath = Join-Path $Desktop ($AppLabel + ".lnk")

if (-not (Test-Path $ExePath)) {
    Write-Error "Exe not found. Run: cd ai_native_learning/desktop; npm run dist"
    exit 1
}
if (-not (Test-Path $IconPath)) {
    Write-Error "Icon not found. Run: uv run python ai_native_learning/desktop/scripts/make_icon.py"
    exit 1
}

$shell = New-Object -ComObject WScript.Shell

# Remove all existing shortcuts to this exe (including garbled names from bad encoding)
Get-ChildItem (Join-Path $Desktop "*.lnk") -ErrorAction SilentlyContinue | ForEach-Object {
    $s = $shell.CreateShortcut($_.FullName)
    if ($s.TargetPath -like "*AiNativeLearning.exe*") {
        Remove-Item $_.FullName -Force
    }
}

$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $ExePath
$shortcut.WorkingDirectory = $WorkDir
$shortcut.WindowStyle = 1
$shortcut.Description = $AppLabel
$shortcut.IconLocation = ($IconPath + ',0')
$shortcut.Save()

$ie4u = Join-Path $env:WINDIR "System32\ie4uinit.exe"
if (Test-Path $ie4u) {
    Start-Process -FilePath $ie4u -ArgumentList "-show" -WindowStyle Hidden
}

Write-Host "Shortcut updated: $ShortcutPath"
Write-Host "Target: $ExePath"
Write-Host "Icon: $($shortcut.IconLocation)"
