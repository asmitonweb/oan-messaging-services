# Integration kits

One kit per module: a patch series for the module's own repository, ready to review and
merge there, plus a README with what it changes, how to apply it, how to configure it and
the evidence it was tested with. The module team owns the merge; nothing here is pushed
to a module repository.

| Kit | Module repository | Base | Sends | Pattern |
|---|---|---|---|---|
| [`oan_a2c_service/`](oan_a2c_service/) | Credit (A2C) | `develop` `f6bb735` | 8 farmer events on the loan lifecycle and agent visits | Connector, `queue_sms` |
| [`consent-management/`](consent-management/) | DPI Consent Manager | `develop` `32bce76` | Consent OTPs | OTP sender (OAN client) |
| [`oan_auth_service/`](oan_auth_service/) | Auth | `develop` `34c7306` | Password-reset codes | OTP sender, connector `send_sms_now` |
| [`oan_grievance_service/`](oan_grievance_service/) | Grievance | `develop` `5bb42c4` | Every SMS row of the existing notification flow | Connector, `queue_sms` |
| [`oan_da_app_services/`](oan_da_app_services/) | DA Services | `develop` `ce156bf` | Farmer SMS for planned and rescheduled visits | Connector, `queue_sms` |
| [`oan_da_app_services/broadcast-channels/`](oan_da_app_services/broadcast-channels/) | DA Services and DA app UI | `develop` `2ae3d13` / UI `4e9dd4d` | Broadcasts on SMS and Telegram: farmers (Communications Officer) and agents | Add-on Frappe app `da_broadcast_channels` + one UI patch; connector `queue_sms`, Bot Services `/broadcasts` |
| [`esignet/`](esignet/) | eSignet (Java) | no source available | Login OTPs | OTP sender: `OanOtpSender`, a JDK-only Java class with tests |

The DA broadcast kit is not a patch to `da_services`: it is an add-on app installed next to it, so
nothing in `da_services` changes until the DA team folds it in (see its README).

The eSignet kit is not a patch: eSignet's source was not available, so it is the sender
the eSignet team calls at its OTP delivery point (guide 15).

## Apply a kit

```
cd <module repository>
git checkout -b oan-messaging <base commit from the table>
git am <path>/integrations/kits/<module>/*.patch
```

If your branch has moved on, apply the kit to the base commit first, then rebase. Each
kit is one commit with a conventional title, so it passes the shared commit-title check.

## What every Frappe kit needs on the site

1. `oan_messaging_connector` installed (`develop` at `4ad8507` or later; the DA broadcast
   kit needs `96fc5d1` or later). The Credit,
   Grievance and DA kits add it to `required_apps` and fetch it in the module's CI; until
   the connector is published under the organisation, set the repository variable
   `OAN_MESSAGING_CONNECTOR_REPO` to a URL that CI can clone. The team is creating the
   connector's shared repository: once it exists, set that variable to its URL in the
   Credit, Grievance and DA repositories (or change the default URL in their `ci.yml`).
2. The site's caller registered by the OAN operator, with the purposes the site sends:
   `transactional` for module notices, plus `otp` where the Auth kit sends reset codes
   (Auth runs on every module site and uses that site's token).
3. `oan_messaging_enabled`, `oan_messaging_base_url`, `oan_messaging_token` (and
   `oan_messaging_ca_bundle` for a private CA) in `site_config.json` or OAN Messaging
   Settings; the scheduler and a `short` queue worker running.

Explicit calls are the integration. The connector's `send_sms` hook (handoff guide 03 §7)
is only an interim option for code that still calls Frappe's SMS sender; with these kits
applied, Grievance and Auth no longer need it.

## How the kits were checked (17 November 2026)

Each kit was applied to its base commit and checked with the module's own tooling:

* **Lint:** the module's pre-commit hooks (ruff, ruff-format, prettier, gitleaks) on
  every changed file; Semgrep with Frappe's rules, `r/python.lang.correctness` and the
  shared OAN rules (the module's CI set). The Consent Manager has no lint configuration;
  its kit was held to ruff's E, F, W, I, UP, B, RUF, S and BLE rules and Semgrep's
  correctness and security rules.
* **Tests:** the kit's tests, then the module's **full** suite with and without the kit
  on Frappe 16.56.0 / Python 3.14.10, comparing the failure sets.
* **End to end** (Credit, Consent, Auth): against a real OAN Messaging Service over HTTPS
  with the fake 8028 gateway and delivery callbacks.

Results are in each kit's README and in `docs/evidence/`. An independent code review of
the kits followed; its findings and fixes are listed in
`docs/evidence/INTEGRATION-KITS-2026-11-17.md`.
