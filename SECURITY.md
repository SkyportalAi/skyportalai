# Security policy

## Reporting a vulnerability

Do not report vulnerabilities through a public GitHub issue.

Email **security@skyportal.ai** or **tech@skyportal.ai** with the affected
version, impact, and reproducible details. Do not include live credentials. The
team will acknowledge the report, investigate it, and coordinate disclosure and
remediation with the reporter.

## Supported versions

Before 1.0, security fixes are made on the latest released version. Upgrade to
the latest release before reporting a problem that may already be resolved.

## Credential safety

- API keys are Bearer credentials. API clients refuse cleartext remote targets;
  plain HTTP is allowed only for loopback development unless the explicit
  `SKYPORTAL_ALLOW_INSECURE=1` escape hatch is set.
- The interactive client does not follow HTTP redirects for authenticated API
  calls, preventing authorization headers from crossing origins.
- Saved CLI credentials are created atomically with user-only permissions.
- The observability agent redacts credential-like config and tag values before
  writing its catalog or delivery queue.

Never paste a real key into an issue, pull request, test fixture, or log.
