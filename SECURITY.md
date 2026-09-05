# Security policy

## Scope

ResumeProof 0.1 performs only local simulated effects. It contains no HTTP client, credentials, payment integration, or messaging integration. Do not adapt the demo to a real write API without adding authentication, authorization, provider-native idempotency, request limits, audit retention, and human reconciliation.

Action fixture content is untrusted data, never instructions. The CLI parses it as JSON and does not invoke a shell. Database paths are explicitly supplied by the operator. Approval actor names are audit labels, not authenticated identities.

SHA-256 receipt hashes detect accidental changes inside this model; they are not signatures and do not protect against an attacker who can rewrite both content and hashes. Protect database files with operating-system access controls. Avoid placing credentials or personal data in fixtures, database paths, logs, or issue reports.

## Reporting

Report vulnerabilities privately to the repository owner before public disclosure. Include the version, reproduction steps, impact, and whether the issue can cross the local-simulation boundary. No production security certification is claimed.
