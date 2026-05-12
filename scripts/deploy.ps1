param(
    [Parameter(Mandatory=$true)] [string] $Remote, # user@host
    [Parameter(Mandatory=$true)] [string] $RemotePath,
    [Parameter(Mandatory=$false)] [string] $EnvFile = ".\.env"
)

Write-Host "Deploying to $Remote:$RemotePath"

# Ensure scp/rsync available (assumes OpenSSH client is installed)
$rsync = Get-Command rsync -ErrorAction SilentlyContinue
if (-not $rsync) { Write-Host "rsync not found; please install or run from WSL/git-bash"; exit 1 }

# Use WSL/rsync semantics on Windows; call from Git Bash / WSL recommended
& rsync -avz --delete --exclude '.venv' --exclude 'memory_db' --exclude 'logs' --exclude '.git' ./ "$Remote:$RemotePath/tmp_deploy/"

Write-Host "Copying $EnvFile to remote /tmp/pridurok.env"
& scp $EnvFile "$Remote:/tmp/pridurok.env"

Write-Host "Finalizing on remote and restarting service (requires sudo)"
$sshCmd = "sudo rsync -av --delete $RemotePath/tmp_deploy/ $RemotePath/ && sudo mv /tmp/pridurok.env $RemotePath/.env && perl -0pi -e 's/^(?:\\xEF\\xBB\\xBF)+//' $RemotePath/*.py || true && sudo systemctl restart pridurok.service && sudo systemctl status pridurok.service --no-pager -l && sudo journalctl -u pridurok.service -n 200 --no-pager"
& ssh $Remote $sshCmd

Write-Host "Deploy finished."
