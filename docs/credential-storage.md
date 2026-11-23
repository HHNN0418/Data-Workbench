# Local credentials

`Protect-CTNSecrets.ps1 -Mode migrate` migrates the six infrastructure credential
files in `data/secrets` to Windows current-user DPAPI. It verifies encryption,
creates encrypted recovery copies in `data/secrets/encrypted-backups/<timestamp>`,
and replaces each canonical file atomically. Existing Agent/provider keys also
receive restricted ACLs. No credential values are printed.

Run the migration under the Windows account that runs CTN. Startup and diagnostic
code reads both legacy text and DPAPI files during rollout. A replacement failure
keeps its original file and verified encrypted recovery copy; rerunning migration
is safe.

Docker cannot decrypt Windows DPAPI. `Protect-CTNSecrets.ps1 -Mode prepare`
materializes only its five required inputs in `data/runtime-secrets`, and Compose
mounts those files into `/run/secrets`. The PostgreSQL connection URL stays out of
that directory. Both canonical and runtime directories/files grant access only to
the current Windows account, SYSTEM, and Administrators. The runtime files must
remain present while Docker mounts them and are necessarily plaintext to those
authorized identities. Avoid copying them into reports or backups.

The launcher prepares runtime files before Compose starts. For direct Compose
commands, run `Protect-CTNSecrets.ps1 -Mode prepare` first. Credential changes
require recreating the affected containers so their secret mounts refresh.

To restore a saved credential set, run:

```powershell
.\Protect-CTNSecrets.ps1 -Mode restore -Backup 'C:\path\to\CTN\data\secrets\encrypted-backups\<timestamp>'
```

Recovery keeps canonical files encrypted and refreshes Docker runtime inputs.
DPAPI recovery requires the original Windows user profile; moving to another
machine/account requires provisioning the credentials again.
