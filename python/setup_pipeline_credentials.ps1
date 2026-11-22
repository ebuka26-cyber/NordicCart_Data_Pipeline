# S10: credentials for the hourly pipeline on the REAL database. Run it yourself, only after explicit authorization:
#     powershell -NoProfile -ExecutionPolicy Bypass -File python\setup_pipeline_credentials.ps1
# 1. asks for the postgres password (masked; used only by the psql child processes of this script)
# 2. generates a random 32-character password for nordiccart_scheduler and nordiccart_extractor (V008 login roles)
#    and sets it with ALTER ROLE, sent on stdin (never on a command line) with log_statement = none for the session
# 3. checks that each role can log in
# 4. writes config\.env.scheduler and config\.env.extractor (git-ignored: .env.*), readable only by the current user
# No password is printed. It does not enable the scheduled task and does not change run_pipeline_hourly.cmd.
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$psql = "C:\Program Files\PostgreSQL\18\bin\psql.exe"
$target = @{ Host = "localhost"; Port = "5432"; Db = "nordiccart_oltp" }

function New-Secret {
    $bytes = New-Object byte[] 48
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    (([Convert]::ToBase64String($bytes)) -replace '[^A-Za-z0-9]', '').Substring(0, 32)
}

function Invoke-Psql([string]$user, [string]$password, [string]$sql) {
    $env:PGPASSWORD = $password
    try {
        $out = $sql | & $psql -h $target.Host -p $target.Port -U $user -d $target.Db -X -q -t -A -v ON_ERROR_STOP=1 -f -
        if ($LASTEXITCODE -ne 0) { throw "psql as $user failed (exit $LASTEXITCODE)" }
        return $out
    } finally { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
}

$secure = Read-Host "postgres password for localhost:5432 (not shown, not stored)" -AsSecureString
$admin = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
$identity = Invoke-Psql "postgres" $admin "SELECT system_identifier FROM pg_control_system();"
if ($identity.Trim() -ne "7663257437785384692") { throw "unexpected server (cluster $identity): nothing changed" }

foreach ($role in "nordiccart_scheduler", "nordiccart_extractor") {
    $secret = New-Secret
    Invoke-Psql "postgres" $admin "SET log_statement = 'none'; ALTER ROLE $role PASSWORD '$secret';" | Out-Null
    $who = Invoke-Psql $role $secret "SELECT current_user;"
    if ($who.Trim() -ne $role) { throw "$role cannot log in" }
    $file = Join-Path $repo "config\.env.$($role -replace 'nordiccart_', '')"
    $text = "PGHOST=$($target.Host)`nPGPORT=$($target.Port)`nPGDATABASE=$($target.Db)`nPGUSER=$role`nPGPASSWORD=$secret`n"
    [IO.File]::WriteAllText($file, $text, (New-Object Text.UTF8Encoding($false)))
    icacls $file /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
    Write-Host "$role : password set, login checked, $file written" -ForegroundColor Green
    Remove-Variable secret
}
Remove-Variable admin
Write-Host "Done. The scheduled task is still disabled; see docs\test_reports\S10_hourly_pipeline_report.md for activation." -ForegroundColor Cyan
