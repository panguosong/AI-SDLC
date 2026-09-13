param(
  [ValidateSet("winget", "choco")]
  [string]$PackageManager = "winget",
  [switch]$ExpectInstallFailure
)

$ErrorActionPreference = "Stop"

function Write-Utf8NoBom {
  param([string]$Path, [string]$Value)
  [IO.File]::WriteAllText($Path, $Value, [Text.UTF8Encoding]::new($false))
}

function Get-NormalizedPath {
  param([string]$Path)
  return ([IO.Path]::GetFullPath($Path)).TrimEnd([char[]]"\/").ToLowerInvariant()
}

function Test-ExactEnvironmentValue {
  param($Actual, $Expected)
  if ($null -eq $Expected) {
    return $null -eq $Actual
  }
  return [string]$Actual -ceq [string]$Expected
}

function Restore-ProcessEnvironmentValue {
  param([string]$Name, $Value)
  if ($null -eq $Value) {
    Remove-Item "Env:$Name" -ErrorAction SilentlyContinue
  } else {
    Set-Item "Env:$Name" $Value
  }
}

function New-MinimalWheel {
  param([string]$Root)

  $wheelRoot = Join-Path $Root "wheel-root"
  $packageRoot = Join-Path $wheelRoot "dummy_ai_sdlc"
  $distInfo = Join-Path $wheelRoot "dummy_ai_sdlc-3.1.0.dist-info"
  New-Item -ItemType Directory -Force -Path $packageRoot, $distInfo | Out-Null
  Write-Utf8NoBom -Path (Join-Path $packageRoot "__init__.py") -Value @'
def main():
    print("3.1.0")
'@
  Write-Utf8NoBom -Path (Join-Path $distInfo "METADATA") -Value @'
Metadata-Version: 2.1
Name: dummy-ai-sdlc
Version: 3.1.0
'@
  Write-Utf8NoBom -Path (Join-Path $distInfo "WHEEL") -Value @'
Wheel-Version: 1.0
Generator: ai-sdlc-bootstrap-e2e
Root-Is-Purelib: true
Tag: py3-none-any
'@
  Write-Utf8NoBom -Path (Join-Path $distInfo "entry_points.txt") -Value @'
[console_scripts]
ai-sdlc = dummy_ai_sdlc:main
'@
  Write-Utf8NoBom -Path (Join-Path $distInfo "RECORD") -Value @'
dummy_ai_sdlc/__init__.py,,
dummy_ai_sdlc-3.1.0.dist-info/METADATA,,
dummy_ai_sdlc-3.1.0.dist-info/WHEEL,,
dummy_ai_sdlc-3.1.0.dist-info/entry_points.txt,,
dummy_ai_sdlc-3.1.0.dist-info/RECORD,,
'@
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $wheelPath = Join-Path $Root "dummy_ai_sdlc-3.1.0-py3-none-any.whl"
  [IO.Compression.ZipFile]::CreateFromDirectory($wheelRoot, $wheelPath)
  return $wheelPath
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
  throw "This replay must run on a native Windows job."
}
if ($ExpectInstallFailure -and $PackageManager -ne "winget") {
  throw "The expected-failure replay is defined only for winget."
}

$realPython = (& py -3.11 -c "import sys; print(sys.executable)").Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $realPython)) {
  throw "The Windows CI orchestrator Python is unavailable."
}
$windowsPowerShell = (Get-Command powershell -ErrorAction Stop).Source
$scenario = if ($ExpectInstallFailure) { "winget-expected-failure" } else { "$PackageManager-success" }

$evidenceRoot = Join-Path $env:RUNNER_TEMP "windows-clean-online-user-e2e-evidence"
$root = Join-Path $env:RUNNER_TEMP "windows-python-bootstrap-replay-$scenario"
Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $evidenceRoot, $root | Out-Null
$shimRoot = Join-Path $root "shims"
$installRoot = Join-Path $root "runtime"
$eventLog = Join-Path $root "python-bootstrap-$scenario-events.txt"
$fakeLocalAppData = Join-Path $root "local-app-data"
$fakeProgramFiles = Join-Path $root "program-files"
New-Item -ItemType Directory -Force -Path $shimRoot, $fakeLocalAppData, $fakeProgramFiles | Out-Null

Write-Utf8NoBom -Path (Join-Path $shimRoot "py.cmd") -Value @'
@echo off
echo py %*>>"%FAKE_BOOTSTRAP_LOG%"
exit /b 1
'@
if ($ExpectInstallFailure) {
  Write-Utf8NoBom -Path (Join-Path $shimRoot "winget.cmd") -Value @'
@echo off
echo package-manager-install winget Python.Python.3.11>>"%FAKE_BOOTSTRAP_LOG%"
exit /b 23
'@
} elseif ($PackageManager -eq "winget") {
  Write-Utf8NoBom -Path (Join-Path $shimRoot "winget.cmd") -Value @'
@echo off
echo package-manager-install winget Python.Python.3.11>>"%FAKE_BOOTSTRAP_LOG%"
set "PYTHON_PARENT=%LOCALAPPDATA%\Programs\Python"
set "PYTHON_TARGET=%PYTHON_PARENT%\Python311"
if not exist "%PYTHON_TARGET%" mkdir "%PYTHON_TARGET%"
xcopy "%FAKE_INSTALLED_PYTHON_ROOT%\*" "%PYTHON_TARGET%\" /E /I /Q /Y >nul
if errorlevel 1 exit /b %ERRORLEVEL%
if not exist "%PYTHON_TARGET%\python.exe" exit /b 1
exit /b 0
'@
} else {
  Write-Utf8NoBom -Path (Join-Path $shimRoot "choco.cmd") -Value @'
@echo off
echo package-manager-install choco python311>>"%FAKE_BOOTSTRAP_LOG%"
if not exist "%FAKE_MACHINE_PYTHON_ROOT%" mkdir "%FAKE_MACHINE_PYTHON_ROOT%"
xcopy "%FAKE_INSTALLED_PYTHON_ROOT%\*" "%FAKE_MACHINE_PYTHON_ROOT%\" /E /I /Q /Y >nul
if errorlevel 1 exit /b %ERRORLEVEL%
if not exist "%FAKE_MACHINE_PYTHON_ROOT%\python.exe" exit /b 1
"%FAKE_WINDOWS_POWERSHELL%" -NoProfile -Command "[Environment]::SetEnvironmentVariable('Path', $env:FAKE_MACHINE_PYTHON_ROOT + ';' + $env:FAKE_ISOLATED_MACHINE_PATH, 'Machine')"
if errorlevel 1 exit /b %ERRORLEVEL%
exit /b 0
'@
}

$wheelPath = New-MinimalWheel -Root $root
$fakeMachinePythonRoot = Join-Path $root "machine-python\Python311"
$saved = @{
  ProcessPath = $env:Path
  MachinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
  UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
  Python = $env:PYTHON
  NoIndex = $env:PIP_NO_INDEX
  DisableCheck = $env:PIP_DISABLE_PIP_VERSION_CHECK
  LocalAppData = $env:LOCALAPPDATA
  ProgramFiles = $env:ProgramFiles
  BootstrapLog = $env:FAKE_BOOTSTRAP_LOG
  InstalledPythonRoot = $env:FAKE_INSTALLED_PYTHON_ROOT
  MachinePythonRoot = $env:FAKE_MACHINE_PYTHON_ROOT
  IsolatedMachinePath = $env:FAKE_ISOLATED_MACHINE_PATH
  FakeWindowsPowerShell = $env:FAKE_WINDOWS_POWERSHELL
}
$isolatedPath = "$shimRoot;$env:SystemRoot\System32"
$output = @()
$installerExit = $null
$capturedFailure = $null
$restorationErrors = [Collections.Generic.List[string]]::new()
$preflightPyResolutions = @()
$preflightPythonResolutionCount = $null
$preflightWherePy = @()
$fakePyExit = $null
$standardPythonRootsAbsent = $false
$distributionVersion = $null
$distributionExit = $null
$cliVersion = $null
$cliExit = $null

try {
  Remove-Item Env:PYTHON -ErrorAction SilentlyContinue
  [Environment]::SetEnvironmentVariable("Path", $isolatedPath, "Machine")
  [Environment]::SetEnvironmentVariable("Path", "", "User")
  $env:Path = $isolatedPath
  $env:LOCALAPPDATA = $fakeLocalAppData
  $env:ProgramFiles = $fakeProgramFiles
  $env:FAKE_BOOTSTRAP_LOG = $eventLog
  $env:FAKE_INSTALLED_PYTHON_ROOT = Split-Path -Parent $realPython
  $env:FAKE_MACHINE_PYTHON_ROOT = $fakeMachinePythonRoot
  $env:FAKE_ISOLATED_MACHINE_PATH = $isolatedPath
  $env:FAKE_WINDOWS_POWERSHELL = $windowsPowerShell
  $env:PIP_NO_INDEX = "1"
  $env:PIP_DISABLE_PIP_VERSION_CHECK = "1"

  $expectedPyPath = Get-NormalizedPath (Join-Path $shimRoot "py.cmd")
  $preflightPyCommands = @(Get-Command py -All -ErrorAction SilentlyContinue)
  $preflightPyResolutions = @($preflightPyCommands | ForEach-Object { Get-NormalizedPath $_.Source })
  if ($preflightPyResolutions.Count -lt 1 -or @($preflightPyResolutions | Where-Object { $_ -ne $expectedPyPath }).Count -ne 0) {
    throw "Preflight py resolution escaped the isolated shim root: $($preflightPyResolutions -join ', ')"
  }
  $preflightPythonCommands = @(Get-Command python -All -ErrorAction SilentlyContinue)
  $preflightPythonResolutionCount = $preflightPythonCommands.Count
  if ($preflightPythonResolutionCount -ne 0) {
    throw "Preflight unexpectedly resolved python: $($preflightPythonCommands.Source -join ', ')"
  }
  $whereExe = Join-Path $env:SystemRoot "System32\where.exe"
  $preflightWherePy = @(& $whereExe py 2>$null | Where-Object { $_ })
  $whereExit = $LASTEXITCODE
  $normalizedWherePy = @($preflightWherePy | ForEach-Object { Get-NormalizedPath $_ })
  if ($whereExit -ne 0 -or $normalizedWherePy.Count -ne 1 -or $normalizedWherePy[0] -ne $expectedPyPath) {
    throw "where.exe py did not resolve only the isolated shim: $($preflightWherePy -join ', ')"
  }
  & py -3.11 -c "import sys; print(sys.executable)" | Out-Null
  $fakePyExit = $LASTEXITCODE
  if ($fakePyExit -eq 0) {
    throw "The isolated py.cmd unexpectedly succeeded."
  }

  $localPythonRoot = Join-Path $fakeLocalAppData "Programs\Python"
  $standardPythonRoots = @()
  foreach ($minorVersion in @(14, 13, 12, 11)) {
    $standardPythonRoots += (Join-Path $localPythonRoot "Python3$minorVersion")
    $standardPythonRoots += (Join-Path $fakeProgramFiles "Python3$minorVersion")
  }
  $dynamicPythonRoots = @(
    Get-ChildItem -LiteralPath $localPythonRoot -Directory -Filter "Python3*" -ErrorAction SilentlyContinue
    Get-ChildItem -LiteralPath $fakeProgramFiles -Directory -Filter "Python3*" -ErrorAction SilentlyContinue
  )
  $existingStandardRoots = @($standardPythonRoots | Where-Object { Test-Path -LiteralPath $_ })
  $standardPythonRootsAbsent = $existingStandardRoots.Count -eq 0 -and $dynamicPythonRoots.Count -eq 0
  if (-not $standardPythonRootsAbsent) {
    throw "A standard or dynamic Python root existed before the package-manager transition."
  }

  $output = @(
    & $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File `
      (Join-Path $PSScriptRoot "..\packaging\install_online.ps1") `
      -VenvPath $installRoot `
      -PackageSpec $wheelPath 2>&1
  )
  $installerExit = $LASTEXITCODE
  if ($ExpectInstallFailure) {
    if ($installerExit -eq 0) {
      throw "The test-only expected-failure installer replay unexpectedly succeeded."
    }
  } elseif ($installerExit -ne 0) {
    throw "Windows isolated Python bootstrap failed with exit $installerExit."
  }
} catch {
  $capturedFailure = $_
} finally {
  try {
    [Environment]::SetEnvironmentVariable("Path", $saved.MachinePath, "Machine")
  } catch {
    $restorationErrors.Add("Machine PATH: $_")
  }
  try {
    [Environment]::SetEnvironmentVariable("Path", $saved.UserPath, "User")
  } catch {
    $restorationErrors.Add("User PATH: $_")
  }
  try {
    $env:Path = $saved.ProcessPath
  } catch {
    $restorationErrors.Add("Process PATH: $_")
  }
  try {
    Restore-ProcessEnvironmentValue -Name "LOCALAPPDATA" -Value $saved.LocalAppData
  } catch {
    $restorationErrors.Add("LOCALAPPDATA: $_")
  }
  try {
    Restore-ProcessEnvironmentValue -Name "ProgramFiles" -Value $saved.ProgramFiles
  } catch {
    $restorationErrors.Add("ProgramFiles: $_")
  }
  foreach ($extraEnvironmentValue in @(
    @{ Name = "PYTHON"; Value = $saved.Python },
    @{ Name = "PIP_NO_INDEX"; Value = $saved.NoIndex },
    @{ Name = "PIP_DISABLE_PIP_VERSION_CHECK"; Value = $saved.DisableCheck },
    @{ Name = "FAKE_BOOTSTRAP_LOG"; Value = $saved.BootstrapLog },
    @{ Name = "FAKE_INSTALLED_PYTHON_ROOT"; Value = $saved.InstalledPythonRoot },
    @{ Name = "FAKE_MACHINE_PYTHON_ROOT"; Value = $saved.MachinePythonRoot },
    @{ Name = "FAKE_ISOLATED_MACHINE_PATH"; Value = $saved.IsolatedMachinePath },
    @{ Name = "FAKE_WINDOWS_POWERSHELL"; Value = $saved.FakeWindowsPowerShell }
  )) {
    try {
      Restore-ProcessEnvironmentValue `
        -Name $extraEnvironmentValue.Name `
        -Value $extraEnvironmentValue.Value
    } catch {
      $restorationErrors.Add("$($extraEnvironmentValue.Name): $_")
    }
  }
}

$machinePathRestored = Test-ExactEnvironmentValue `
  ([Environment]::GetEnvironmentVariable("Path", "Machine")) $saved.MachinePath
$userPathRestored = Test-ExactEnvironmentValue `
  ([Environment]::GetEnvironmentVariable("Path", "User")) $saved.UserPath
$processPathRestored = Test-ExactEnvironmentValue $env:Path $saved.ProcessPath
$localAppDataRestored = Test-ExactEnvironmentValue $env:LOCALAPPDATA $saved.LocalAppData
$programFilesRestored = Test-ExactEnvironmentValue $env:ProgramFiles $saved.ProgramFiles
if (-not $machinePathRestored) { $restorationErrors.Add("Machine PATH equality check failed.") }
if (-not $userPathRestored) { $restorationErrors.Add("User PATH equality check failed.") }
if (-not $processPathRestored) { $restorationErrors.Add("Process PATH equality check failed.") }
if (-not $localAppDataRestored) { $restorationErrors.Add("LOCALAPPDATA equality check failed.") }
if (-not $programFilesRestored) { $restorationErrors.Add("ProgramFiles equality check failed.") }

$outputText = $output -join "`n"
$outputPath = Join-Path $evidenceRoot "python-bootstrap-$scenario-output.txt"
Write-Utf8NoBom -Path $outputPath -Value ($outputText + "`n")
$eventsPath = Join-Path $evidenceRoot "python-bootstrap-$scenario-events.txt"
if (Test-Path -LiteralPath $eventLog) {
  Copy-Item -LiteralPath $eventLog -Destination $eventsPath -Force
} else {
  Write-Utf8NoBom -Path $eventsPath -Value ""
}
$events = @(Get-Content -LiteralPath $eventsPath -ErrorAction SilentlyContinue)
$installEvents = @($events | Where-Object { $_ -like "package-manager-install *" })
$installedPython = if ($PackageManager -eq "winget") {
  Join-Path $fakeLocalAppData "Programs\Python\Python311\python.exe"
} else {
  Join-Path $fakeMachinePythonRoot "python.exe"
}
$runtimeAbsent = -not (Test-Path -LiteralPath $installedPython)
$venvAbsent = -not (Test-Path -LiteralPath $installRoot)

if (-not $capturedFailure -and $installEvents.Count -ne 1) {
  $capturedFailure = "The Windows replay did not execute exactly one package-manager transition."
}
if (-not $capturedFailure -and -not $ExpectInstallFailure) {
  if (-not (Test-Path -LiteralPath $installedPython)) {
    $capturedFailure = "The Windows replay did not materialize Python through $PackageManager."
  } else {
    $runtimeDisplay = if ($PackageManager -eq "winget") { $installedPython } else { "python" }
    foreach ($marker in @(
      "No Python 3.11+ detected. Attempting online installation",
      "Using Python runtime: $runtimeDisplay",
      "Online installation completed"
    )) {
      if (-not $outputText.Contains($marker)) {
        $capturedFailure = "Installer output missing bootstrap marker: $marker"
        break
      }
    }
  }
}

$venvPython = Join-Path $installRoot "Scripts\python.exe"
$cliExe = Join-Path $installRoot "Scripts\ai-sdlc.exe"
if (-not $capturedFailure -and -not $ExpectInstallFailure) {
  if (-not (Test-Path -LiteralPath $venvPython) -or -not (Test-Path -LiteralPath $cliExe)) {
    $capturedFailure = "The Windows replay did not create both absolute venv executables."
  } else {
    $distributionOutput = @(& $venvPython -c "import importlib.metadata; print(importlib.metadata.version('dummy-ai-sdlc'))" 2>&1)
    $distributionExit = $LASTEXITCODE
    $distributionVersion = ($distributionOutput -join "`n").Trim()
    $cliOutput = @(& $cliExe --version 2>&1)
    $cliExit = $LASTEXITCODE
    $cliVersion = ($cliOutput -join "`n").Trim()
    if ($distributionExit -ne 0 -or $distributionVersion -ne "3.1.0") {
      $capturedFailure = "Installed distribution version was '$distributionVersion' with exit $distributionExit."
    } elseif ($cliExit -ne 0 -or $cliVersion -ne "3.1.0") {
      $capturedFailure = "Installed CLI version was '$cliVersion' with exit $cliExit."
    }
  }
}
if (-not $capturedFailure -and $ExpectInstallFailure) {
  if ($null -eq $installerExit -or $installerExit -eq 0 -or -not $runtimeAbsent -or -not $venvAbsent) {
    $capturedFailure = "Expected failure did not preserve the no-runtime/no-venv state."
  }
}
$executionFailure = if ($capturedFailure) { $capturedFailure.ToString() } else { $null }
$restorationFailures = @($restorationErrors)

$evidence = [ordered]@{
  platform = "Windows"
  scenario = $scenario
  package_manager = $PackageManager
  expected_install_failure = [bool]$ExpectInstallFailure
  installer_exit = $installerExit
  manager_transition_count = $installEvents.Count
  preflight_py_resolutions = $preflightPyResolutions
  preflight_python_resolution_count = $preflightPythonResolutionCount
  preflight_where_py = $preflightWherePy
  fake_py_exit = $fakePyExit
  standard_python_roots_absent = $standardPythonRootsAbsent
  machine_path_restored = $machinePathRestored
  user_path_restored = $userPathRestored
  process_path_restored = $processPathRestored
  local_app_data_restored = $localAppDataRestored
  program_files_restored = $programFilesRestored
  distribution_version = $distributionVersion
  distribution_version_exit = $distributionExit
  cli_version = $cliVersion
  cli_version_exit = $cliExit
  runtime_absent = $runtimeAbsent
  venv_absent = $venvAbsent
  execution_failure = $executionFailure
  restoration_failures = $restorationFailures
}
$evidencePath = Join-Path $evidenceRoot "python-bootstrap-$scenario.json"
Write-Utf8NoBom -Path $evidencePath -Value (($evidence | ConvertTo-Json -Depth 4) + "`n")

$finalFailureParts = @()
if ($executionFailure) {
  $finalFailureParts += "Execution failure: $executionFailure"
}
if ($restorationFailures.Count -ne 0) {
  $finalFailureParts += "Environment restoration failures: $($restorationFailures -join '; ')"
}
$finalFailure = if ($finalFailureParts.Count -ne 0) { $finalFailureParts -join " | " } else { $null }
if ($finalFailure) {
  throw $finalFailure
}
Write-Host "WINDOWS_PYTHON_BOOTSTRAP_OK"
Write-Host $evidencePath
