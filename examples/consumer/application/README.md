# Example application consumer

The repository owns the application image and `deploy/platform.yml`. Create
`deploy/secrets.staging.sops.yaml` with SOPS and the target host's age
recipient; never commit plaintext secrets or an age identity.
