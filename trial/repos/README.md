# trial/repos/

The Credit and Consent Manager trial stacks mount this folder. Put these checkouts here
(they are ignored by git):

| Folder | Source | Base commit | Then |
|---|---|---|---|
| `oan_messaging_connector` | the `oan_messaging_connector` repository | branch `develop` | nothing |
| `oan_a2c_service` | your Credit repository | `f6bb735` | `git am ../../../integrations/kits/oan_a2c_service/*.patch` |
| `oan_auth_service` | your Auth repository | `34c7306` | optionally `git am ../../../integrations/kits/oan_auth_service/*.patch` (A2C requires Auth) |
| `consent-management` | your DPI repository | `b6d5e45` (`main`) | the trial's end-to-end test uses the `main` API. The kit itself targets `develop` (`32bce76`); see `integrations/kits/consent-management/README.md` |

If your branch has moved past the base commit, apply the kit to the base on a new branch
first, then rebase.
