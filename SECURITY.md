# Security policy

## Reporting a vulnerability

Please report it privately, through GitHub: open the
[Security tab](https://github.com/embeddedci-com/emi-analyzer/security) and click
**Report a vulnerability**, or go straight to
<https://github.com/embeddedci-com/emi-analyzer/security/advisories/new>.

Do not open a public issue for it. Say what is affected, how to reproduce it, and what an
attacker gains. A board file that triggers it helps, if you can share one.

We will acknowledge the report, keep you updated in the advisory, and credit you when the fix is
published unless you would rather we did not.

## Supported versions

Only the latest release gets fixes. The desktop app, the standalone `emi-local` binary and the
worker image of the same version are released together, so update all of them.

## In scope

- **The desktop app and `emi-local`**: the local server has no sign-in and relies on listening
  only on loopback, the Host and Origin checks, signed file links and the worker key. Anything
  that lets a web page, another user on the machine or the network reach or drive it is in scope.
- **The worker**: it parses boards from other people (KiCad files, Gerber and Excellon, rules
  files) and runs ngspice, nec2c and openEMS on what it read. Anything a crafted board can do
  beyond producing a wrong result, such as running commands, reading or writing files outside
  its scratch directory, or exhausting the host, is in scope.
- **`emi-server`** and the `server/emi` package it is built from: authentication, API keys,
  access between organizations and presigned storage URLs.
- **The KiCad plugin**: how it finds the app and what it sends there.
- **The release process**: the workflows, the published installers and the
  `ghcr.io/embeddedci-com/emi-worker` image.

Out of scope: the analysis results themselves (an EMI estimate that is wrong is a bug; please
open an issue), and vulnerabilities in third-party programs the image carries, which belong
with their own projects unless the way we use them makes them reachable.
