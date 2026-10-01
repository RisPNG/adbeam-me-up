param(
    [switch]$Hidden,
    [switch]$NoLaunch
)

$ErrorActionPreference = 'Stop'
$env:PSModulePath = Join-Path $PSHOME 'Modules'
Remove-Item Env:PYTHONHOME, Env:PYTHONPATH, Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$architecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
$staging = $null
$transcribing = $false
$exitCode = 1
try {
    $logs = Join-Path $root 'bin\win\logs'
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    Start-Transcript -Path (Join-Path $logs 'setup.log') -Append | Out-Null
    $transcribing = $true
    if ($architecture -ne 'AMD64') {
        throw 'The pinned MsPy 3.11.14 Windows runtime supports x64 only.'
    }
    $runtime = Join-Path $root 'int\win\MsPy-3_11_14'
    $python = Join-Path $runtime 'python.exe'
    $valid = $false
    if (Test-Path -LiteralPath $python -PathType Leaf) {
        try {
            & $python -c 'import sys; assert sys.version_info[:3] == (3, 11, 14)'
            $valid = $LASTEXITCODE -eq 0
        } catch {
            $valid = $false
        }
    }
    if (-not $valid) {
        $parent = Join-Path $root 'int\win'
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
        $staging = Join-Path $parent ('.mspy-bootstrap-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $staging | Out-Null
        $archive = Join-Path $staging 'MsPy-3_11_14-win.zip'
        $url = 'https://github.com/RisPNG/MsPy/releases/download/3.11.14/MsPy-3_11_14-win.zip'
        $expected = '575162a0ecccb7c8a419d758174733c31a0f5ac0ae534e17c1e31e1fe0567733'
        Write-Output 'Downloading portable Python 3.11.14...'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing -TimeoutSec 300
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
            throw 'The runtime download did not match its published SHA-256 digest. Run setup again.'
        }
        Expand-Archive -LiteralPath $archive -DestinationPath $staging
        $extracted = Join-Path $staging 'MsPy-3_11_14\python.exe'
        if (-not (Test-Path -LiteralPath $extracted -PathType Leaf)) {
            throw 'The runtime archive did not contain the expected Python interpreter.'
        }
        & $extracted -c 'import sys; assert sys.version_info[:3] == (3, 11, 14)'
        if ($LASTEXITCODE -ne 0) {
            throw 'The downloaded Python runtime could not start.'
        }
        $previous = Join-Path $staging 'previous-runtime'
        if (Test-Path -LiteralPath $runtime) {
            Move-Item -LiteralPath $runtime -Destination $previous
        }
        try {
            Move-Item -LiteralPath (Join-Path $staging 'MsPy-3_11_14') -Destination $runtime
        } catch {
            if (Test-Path -LiteralPath $previous) {
                Move-Item -LiteralPath $previous -Destination $runtime
            }
            throw
        }
    }
    $pythonArguments = @('-u', (Join-Path $root 'tools\setup_portable.py'))
    if ($Hidden) { $pythonArguments += '--hidden' }
    if ($NoLaunch) { $pythonArguments += '--no-launch' }
    & $python @pythonArguments
    $exitCode = $LASTEXITCODE
} catch {
    Write-Error $_ -ErrorAction Continue
    $exitCode = 1
} finally {
    if ($staging -and (Test-Path -LiteralPath $staging)) {
        Remove-Item -LiteralPath $staging -Recurse -Force
    }
    if ($transcribing) {
        Stop-Transcript | Out-Null
    }
}
exit $exitCode
