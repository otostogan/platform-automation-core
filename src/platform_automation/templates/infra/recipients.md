# SOPS recipients

Public age recipients for the platform hosts. These are public keys:
committing them is safe and intended. The private halves are not here.

```
{{host}}  {{recipient_host}}
recovery  {{recipient_recovery}}
```

Every application encrypts its secrets to **both** recipients: the host it
deploys to and recovery. `platform new app` reads this file and writes the
application's `.sops.yaml` from it.

## Why two

`platform_cli_minimum_age_recipients: 2` is enforced on every path that
decrypts secrets: bundle build, `platform deploy`, `platform rollback`, and
boot secret recovery. A file encrypted to one recipient will not deploy, will
not roll back, and its secrets will not be restored after a reboot.

## Where the private halves live

| Recipient | Private half                                                              |
| --------- | ------------------------------------------------------------------------- |
| host      | the server at `/etc/platform/keys/age.key`, and the operator's controller |
| recovery  | the company secret store, nowhere else                                    |
