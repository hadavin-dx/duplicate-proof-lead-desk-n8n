# Security

This is a demo. By default it uses made-up data and demo stand-ins only, binds to this computer's own address, has no login (the
portal also has buttons that write to the lead book: see `docs/portal.md`), and is not meant to run anywhere
reachable by other people. Do not put real customer data into it.

## Reporting a problem

Send a report by e-mail to sales@hadavin.com (it reaches the author), or use GitHub's
private vulnerability reporting: the "Report a vulnerability" button on the Security tab of this repository.
Please do not describe the problem in a public place first.

## Supported versions and answer time

Only the latest state of the `main` branch is supported. Reports are answered on a best-effort basis, with no promised time.

## What is and is not covered

* There are no secrets in this repository and none are needed: with the default settings the demo makes no call to
  any real service and talks only to demo servers on the same computer. These settings, each off by default, send
  traffic to a real service over the network, and each needs a secret that you keep in a file outside the repository:
  `DESK_QUALIFIER=live_claude` (a real AI model; key file, prices and spend caps; `docs/settings.md`, "Live model step"),
  `DESK_CRM=hubspot` (a real HubSpot account; CRM key file and a call cap), `DESK_OUTBOX=smtp_provider` (a real mail
  provider; mail password file and a send cap) and the mailbox reader of `scripts/run_real_check.py` (IMAP, which also
  needs a password file). The CRM key file and the mail password files are secrets just like the AI key file: never
  commit them, never paste them into a command line, and keep them in a file, not in a variable. The one exception is the one-shot script `scripts/hubspot_probe.py` (`docs/hubspot-probe.md`): it reads its token from an environment variable that you set in your own terminal window for that single run. If you find something that looks like a secret,
  report it as above. `scripts/run_live_check.py` also uses a real model key, and `scripts/n8n_api.py` reads an n8n API key from the file named in
  `N8N_API_KEY_FILE` (same rule: a file outside the repository, never a variable or a command line value). The demo-server and plain-SMTP settings (`DESK_FAKES_URL`, `DESK_SMTP_HOST`, `DESK_OUTBOX=smtp`) accept only this computer's own address, so a stand-in can never be a real host. With any real service set, the desk starts only on a loopback `DESK_BIND`: it has no sign-in, and anyone who could reach the port could create contacts in the CRM, send mail or spend model money.
* Outside contributions (pull requests) are not accepted.
* The software may be downloaded and run for evaluation and testing only (see `LICENSE`).
* Automatic checks (`.github/workflows/`; no result of them is claimed here) look for known bug patterns and secret formats. They do not prove the code
  is free of problems, and they do not keep anything private once it has been published.
