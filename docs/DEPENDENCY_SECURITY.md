# Dependency security baseline

CI runs `pip-audit` against the pinned Python requirements and blocks every
new known vulnerability. Direct dependencies and compatible transitive packages
must be upgraded before merge whenever a fixed release exists.

The CI command temporarily ignores five Starlette advisory identifiers. The
current latest compatible FastAPI release requires Starlette below 1.0, while
those advisories declare their fixes only in Starlette 1.x. This is a documented
compatibility exception, not a claim that the findings are fixed. Dependabot
checks weekly, and the exception must be removed when FastAPI supports the fixed
Starlette line.

The application reduces exposure while that exception exists: uploaded content
is size constrained, filenames and storage paths are validated, protected actions
remain behind approval, and production is not configured as a general-purpose
file server. Any new endpoint that parses multipart forms or serves user-controlled
files must receive a focused security review.
