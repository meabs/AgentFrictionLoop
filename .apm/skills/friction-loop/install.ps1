# Windows PowerShell: forwards all options to install.py, e.g.  .\install.ps1 --repo C:\code\proj --domain team
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
foreach ($py in @("py", "python", "python3")) {
  if (Get-Command $py -ErrorAction SilentlyContinue) { & $py "$here\install.py" @args; exit $LASTEXITCODE }
}
Write-Error "python 3 is required"; exit 1
