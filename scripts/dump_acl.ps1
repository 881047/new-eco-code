foreach ($p in @('D:\NEW ECO\data', 'D:\NEW ECO\data\neweco.db', 'D:\NEW ECO\logs', 'D:\NEW ECO\logs\app.log')) {
  Write-Output ('=== ' + $p)
  try {
    (Get-Acl -LiteralPath $p).Access | ForEach-Object {
      Write-Output ($_.IdentityReference.ToString() + ' :: ' + $_.FileSystemRights.ToString() + ' :: ' + $_.AccessControlType.ToString() + ' :: inherited=' + $_.IsInherited)
    }
  } catch {
    Write-Output ('读取失败: ' + $_.Exception.Message)
  }
}
