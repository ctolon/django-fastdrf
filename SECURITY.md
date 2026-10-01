# Security policy

## Supported versions

Security fixes are made for the latest release of django-fastdrf. Upgrade to
it before reporting an issue, if you can.

## Reporting a vulnerability

Report a suspected vulnerability privately through GitHub's private
vulnerability reporting:
[open a report](https://github.com/ctolon/django-fastdrf/security/advisories/new)
on the repository's Security tab. Do not open a public issue or pull request
for it.

Include the affected version, the Django, DRF and Python versions, the
settings and classes involved, and the steps to reproduce. Do not include
credentials, personal data or production data.

You will receive an acknowledgement, and the advisory will be published with
the fixed release once a fix is available.

## Scope

django-fastdrf changes how DRF serializes, validates, loads related objects
and renders. It does not replace authentication, permissions, throttling,
queryset scoping or validation that a project defines. A report is in scope
when an optimization makes a response, a validation result or a query differ
from what DRF would produce with the same project code, or exposes data
across requests.
