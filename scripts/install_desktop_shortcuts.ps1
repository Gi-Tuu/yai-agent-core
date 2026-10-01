# 在桌面创建两个 YAI 启动入口：离线演示 / 真实员工（用 pythonw，无控制台黑窗）。
# 自动按脚本位置推断项目根与 venv，可重复执行（覆盖旧快捷方式）。
# 用法：pwsh scripts/install_desktop_shortcuts.ps1
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) {
    throw "找不到 pythonw：$pythonw（先创建 venv 并 uv sync --extra desktop）"
}

$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell

function New-YaiShortcut {
    param([string]$Name, [string]$Arguments)
    $path = Join-Path $desktop ($Name + '.lnk')
    $shortcut = $shell.CreateShortcut($path)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $root
    $shortcut.IconLocation = "$pythonw,0"
    $shortcut.Description = $Name
    $shortcut.Save()
    Write-Host "已创建：$path"
}

New-YaiShortcut -Name 'YAI 数字员工（离线演示）' -Arguments '-m shell.desktop'
New-YaiShortcut -Name 'YAI 数字员工（真实员工）' -Arguments '-m shell.desktop --live'
