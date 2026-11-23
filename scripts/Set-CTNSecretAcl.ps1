param([Parameter(Mandatory=$true)][string]$Path)
$ErrorActionPreference = 'Stop'
$ctnAclItem = Get-Item -LiteralPath $Path -Force
$ctnAclIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().User
$ctnAclSystem = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$ctnAclAdmins = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
if ($ctnAclItem.PSIsContainer) {
    $ctnAcl = [Security.AccessControl.DirectorySecurity]::new()
    $ctnAclInheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
} else {
    $ctnAcl = [Security.AccessControl.FileSecurity]::new()
    $ctnAclInheritance = [Security.AccessControl.InheritanceFlags]::None
}
$ctnAcl.SetAccessRuleProtection($true, $false)
$ctnAcl.SetOwner($ctnAclIdentity)
foreach ($ctnAclPrincipal in @($ctnAclIdentity, $ctnAclSystem, $ctnAclAdmins)) {
    $ctnAclRule = [Security.AccessControl.FileSystemAccessRule]::new(
        $ctnAclPrincipal, [Security.AccessControl.FileSystemRights]::FullControl,
        $ctnAclInheritance, [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow)
    $ctnAcl.AddAccessRule($ctnAclRule)
}
if ($ctnAclItem.PSObject.Methods['SetAccessControl']) {
    $ctnAclItem.SetAccessControl($ctnAcl)
    $ctnAclVerified = $ctnAclItem.GetAccessControl()
} else {
    [IO.FileSystemAclExtensions]::SetAccessControl($ctnAclItem, $ctnAcl)
    $ctnAclVerified = [IO.FileSystemAclExtensions]::GetAccessControl($ctnAclItem)
}
$ctnAclAllowed = @($ctnAclIdentity.Value, $ctnAclSystem.Value, $ctnAclAdmins.Value)
$ctnAclRules = @($ctnAclVerified.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
if (-not $ctnAclVerified.AreAccessRulesProtected -or $ctnAclRules.Count -ne 3) {
    throw 'Credential permissions did not match the required restricted ACL.'
}
foreach ($ctnAclRule in $ctnAclRules) {
    if ($ctnAclRule.IdentityReference.Value -notin $ctnAclAllowed -or
        $ctnAclRule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
        $ctnAclRule.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl) {
        throw 'Unexpected access rule remains on a credential path.'
    }
}
